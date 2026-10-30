"""Order intake tools: functions Claude can call, with their tool definitions."""

import datetime as dt
import json
import re
import secrets
from pathlib import Path
from typing import Annotated, Literal

import anthropic
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator

from src.agents.intake.templates import (
    render_load_confirmation,
    render_missing_info,
    render_quote_approval,
    render_quote_under_review,
)
from src.config.accounts import TRUCKING_EMAIL
from src.services import db, gmail, slack

ROOT = Path(__file__).resolve().parents[3]
RATES = json.loads((ROOT / "data" / "rates.json").read_text(encoding="utf-8"))
ORDER_SPEC = json.loads((ROOT / "data" / "order_spec.json").read_text(encoding="utf-8"))
# Fields an order can be missing, in spec order: every required field plus conditional ones (reefer temperature).
REQUIRED_FIELD_KEYS = [f["field"] for f in ORDER_SPEC["required_fields"] + ORDER_SPEC["conditional_fields"]]

EquipmentType = Literal["dry van", "reefer", "flatbed", "step deck"]


def _state_code(v: str) -> str:
    if not re.fullmatch(r"[A-Za-z]{2}", v.strip()):
        raise ValueError(f"state must be a two-letter code, got {v!r}")
    return v.strip().upper()


def _positive_price(v: float) -> float:
    if v <= 0:
        raise ValueError("quoted_price must be greater than 0")
    return v


StateCode = Annotated[str, AfterValidator(_state_code)]
QuotedPrice = Annotated[float, AfterValidator(_positive_price)]


class Stop(BaseModel):
    model_config = ConfigDict(extra="forbid")

    facility_name: str = Field(description="Facility or company name at this stop.")
    street_address: str = Field(description="Street address only, e.g. '4120 Distriplex Dr'.")
    city: str
    state: StateCode = Field(description="Two-letter US state code, e.g. 'TN'.")
    zip_code: str | None = Field(description="5-digit ZIP code, or null if the order doesn't give one.")
    date: dt.date = Field(description="Calendar date of the pickup or delivery.")
    time_window: str | None = Field(description="Time, window, or appointment as written, e.g. '08:00-12:00'. Null if not given.")

    @field_validator("zip_code")
    @classmethod
    def _zip(cls, v: str | None) -> str | None:
        if v is not None and not re.fullmatch(r"\d{5}", v.strip()):
            raise ValueError(f"zip_code must be 5 digits, got {v!r}")
        return v.strip() if v else None


class LoadOrder(BaseModel):
    """Everything a valid order request must contain (data/order_spec.json), plus optional extras."""

    model_config = ConfigDict(extra="forbid")

    customer_name: str = Field(description="Shipper company name, as it appears in the email signature or document.")
    customer_reference: str = Field(description="The shipper's PO, order, load, or sales order number.")
    pickup: Stop
    delivery: Stop
    commodity: str = Field(description="What is being shipped.")
    weight_lbs: float = Field(description="Total shipment weight in pounds.")
    equipment_type: EquipmentType
    temperature_f: float | None = Field(description="Reefer set point in °F. Required for reefer loads, otherwise null.")
    handling_units: str | None = Field(description="Pallet, carton, or bundle count as written, e.g. '26 pallets'. Null if not given.")
    special_instructions: str | None = Field(description="Handling notes such as load bars, tarps, washout, or reefer run mode. Null if none.")
    contact_name: str | None = Field(description="Shipper contact person. Null if not given.")
    contact_phone: str | None = Field(description="Shipper contact phone. Null if not given.")

    @field_validator("weight_lbs")
    @classmethod
    def _weight(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("weight_lbs must be greater than 0")
        return v

    @model_validator(mode="after")
    def _check(self) -> "LoadOrder":
        if self.delivery.date < self.pickup.date:
            raise ValueError("delivery date is before pickup date")
        if self.equipment_type == "reefer" and self.temperature_f is None:
            raise ValueError("reefer loads need temperature_f")
        return self


class QuoteCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pickup_state: StateCode = Field(description="Two-letter state code of the pickup, e.g. 'CA'.")
    delivery_state: StateCode = Field(description="Two-letter state code of the delivery, e.g. 'CO'.")
    equipment_type: EquipmentType
    quoted_price: QuotedPrice = Field(description="Total price the shipper offered for the load, in USD (e.g. 3600).")


class QuoteApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order: LoadOrder
    quoted_price: QuotedPrice = Field(description="The same quoted price passed to approve_quote, in USD.")


def standard_price(pickup_state: str, delivery_state: str, equipment_type: str) -> float | None:
    """Our standard all-in price for a lane (rates.json), or None if we don't have one."""
    return next(
        (
            r["price"]
            for r in RATES["rates"]
            if (r["pickup_state"], r["delivery_state"], r["equipment"]) == (pickup_state, delivery_state, equipment_type)
        ),
        None,
    )


def _lane(pickup_state: str, delivery_state: str, equipment_type: str) -> str:
    return f"{pickup_state} to {delivery_state}, {equipment_type}"


def _evaluate_quote(pickup_state: str, delivery_state: str, equipment_type: str, quoted_price: float) -> dict:
    """A quote is approved if it's at or above our standard price for the lane."""
    standard = standard_price(pickup_state, delivery_state, equipment_type)
    result = {"lane": _lane(pickup_state, delivery_state, equipment_type), "quoted_price": quoted_price, "standard_price": standard}
    if standard is None:
        return {"approved": False, **result, "reason": "We don't have a standard price for this lane."}
    return {"approved": quoted_price >= standard, **result, "difference": round(quoted_price - standard, 2)}


def _number(v: float | None) -> float | int | None:
    """Store 40000.0 as 40000 so documents read cleanly."""
    return int(v) if v is not None and v.is_integer() else v


def _address(stop: Stop) -> str:
    zip_code = f" {stop.zip_code}" if stop.zip_code else ""
    return f"{stop.facility_name}, {stop.street_address}, {stop.city}, {stop.state}{zip_code}"


def _load_doc(load_id: str, load: LoadOrder, price: float | None, source: dict | None) -> dict:
    """A flat, readable load document: key facts first, empty fields left out."""
    doc = {
        "load_id": load_id,
        # A load on a lane we don't have a price for needs a person to price it.
        "status": "booked" if price is not None else "pending_price",
        "customer": load.customer_name,
        "customer_ref": load.customer_reference,
        "lane": f"{load.pickup.state} to {load.delivery.state}",
        "equipment": load.equipment_type,
        "price": price,
        "pickup": _address(load.pickup),
        "pickup_date": load.pickup.date.isoformat(),
        "pickup_time": load.pickup.time_window,
        "delivery": _address(load.delivery),
        "delivery_date": load.delivery.date.isoformat(),
        "delivery_time": load.delivery.time_window,
        "commodity": load.commodity,
        "weight_lbs": _number(load.weight_lbs),
        "units": load.handling_units,
        "temperature_f": _number(load.temperature_f),
        "contact": ", ".join(p for p in (load.contact_name, load.contact_phone) if p) or None,
        "notes": load.special_instructions,
        "email_id": source["message_id"] if source else None,
        "created_at": dt.datetime.now(dt.timezone.utc),
    }
    return {key: value for key, value in doc.items() if value is not None}


def _send_confirmation(load_id: str, order: dict, price: float | None, source: dict) -> None:
    """Reply in the order's email thread with the confirmation template, and record when it went out."""
    gmail.reply_to_email(TRUCKING_EMAIL, source["message_id"], render_load_confirmation(order, load_id, price))
    db.update_load(load_id, {"confirmation_sent_at": dt.datetime.now(dt.timezone.utc)})


def create_load(source: dict | None = None, **order) -> dict:
    """Validate an order, price it from rates.json, save it as a load, and reply to the shipper with a confirmation.

    `order` is the create_load tool input (see LoadOrder). `source` is the email
    the order came from ({message_id, thread_id, from, subject}); it's supplied by
    our code, not by Claude. Without a source, the load is saved but no email is
    sent. Returns a short summary of the created load.
    """
    if source and (existing := db.find_load(email_id=source["message_id"])):
        return {"load_id": existing["load_id"], "duplicate": True}

    load = LoadOrder.model_validate(order)
    price = standard_price(load.pickup.state, load.delivery.state, load.equipment_type)
    doc = _load_doc(f"LD-{dt.datetime.now():%y%m%d}-{secrets.token_hex(2).upper()}", load, price, source)
    db.insert_load(doc)
    if source:
        _send_confirmation(doc["load_id"], load.model_dump(mode="json"), price, source)
    return {
        "load_id": doc["load_id"],
        "status": doc["status"],
        "lane": _lane(load.pickup.state, load.delivery.state, load.equipment_type),
        "price": price,
        "confirmation_sent": bool(source),
    }


def request_missing_info(missing_fields: list[str], contact_name: str | None, source: dict | None = None) -> dict:
    """Reply to an incomplete order request listing what's missing, and ask for the full order again.

    `missing_fields` are keys from REQUIRED_FIELD_KEYS; our code turns them into
    the reply wording (see templates.MISSING_FIELD_PROMPTS). `source` is the
    order email, supplied by our code. Without it, nothing is sent.
    """
    unknown = [f for f in missing_fields if f not in REQUIRED_FIELD_KEYS]
    if unknown:
        raise ValueError(f"Unknown field keys: {unknown}")
    fields = sorted(set(missing_fields), key=REQUIRED_FIELD_KEYS.index)
    if not fields:
        raise ValueError("missing_fields is empty; a complete order should use create_load.")

    if source:
        gmail.reply_to_email(TRUCKING_EMAIL, source["message_id"], render_missing_info(fields, contact_name))
    return {"missing_fields": fields, "reply_sent": bool(source)}


def approve_quote(**check) -> dict:
    """Check the shipper's quoted price against our standard price for the lane.

    `approved` is True when the quote is at or above our price. A lane we don't
    have a price for is never auto-approved.
    """
    q = QuoteCheck.model_validate(check)
    return _evaluate_quote(q.pickup_state, q.delivery_state, q.equipment_type, q.quoted_price)


def raise_quote_for_approval(source: dict | None = None, **request) -> dict:
    """Post an order whose quote is below our price to Slack for a person to approve or reject,
    then tell the shipper their rate is under review.

    `source` is the order email ({message_id, thread_id, from, subject}),
    supplied by our code. It's shown in the Slack message and is where the
    holding reply goes; without it, no email is sent.
    """
    req = QuoteApprovalRequest.model_validate(request)
    o = req.order
    evaluation = _evaluate_quote(o.pickup.state, o.delivery.state, o.equipment_type, req.quoted_price)
    if evaluation["approved"]:
        raise ValueError("This quote meets our standard price; book it with create_load instead.")

    order = o.model_dump(mode="json")
    slack.send_message(*render_quote_approval(order, evaluation, source))
    # Only tell the shipper it's under review once the team has actually been asked.
    if source:
        gmail.reply_to_email(TRUCKING_EMAIL, source["message_id"], render_quote_under_review(order, req.quoted_price))
    return {"sent_to_slack": True, "reply_sent": bool(source), **evaluation}


CREATE_LOAD_TOOL = {
    "name": "create_load",
    "description": (
        "Create a load from an order request that contains every required detail: customer reference, "
        "pickup and delivery facility addresses with specific dates, commodity, weight in pounds, and "
        "equipment type (plus temperature for reefer loads). Fill fields only from the email and its "
        "attachments; never guess a missing detail. Use null for optional details that aren't given. "
        "If the email includes a quote, call this only after approve_quote returns approved: true."
    ),
    "strict": True,
    "input_schema": anthropic.transform_schema(LoadOrder),
}

REQUEST_MISSING_INFO_TOOL = {
    "name": "request_missing_info",
    "description": (
        "Use when the email is an order request but one or more required details are missing, or are too "
        "vague to book (e.g. 'same pickup as always' instead of an address, 'ASAP' instead of a date, or "
        "a pallet count with no weight). List only the missing fields. The shipper gets a reply asking "
        "them to resend the full order with those details."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "missing_fields": {
                "type": "array",
                "items": {"type": "string", "enum": REQUIRED_FIELD_KEYS},
                "description": "Required fields the order is missing. 'temperature' applies only to reefer loads.",
            },
            "contact_name": {
                "type": ["string", "null"],
                "description": "Shipper contact's name from the email, used to greet them. Null if not given.",
            },
        },
        "required": ["missing_fields", "contact_name"],
        "additionalProperties": False,
    },
}

APPROVE_QUOTE_TOOL = {
    "name": "approve_quote",
    "description": (
        "Check the total price the shipper offered in the email (a quote or budget) against our standard "
        "price for the lane (pickup state, delivery state, equipment). Returns approved: true if the offer "
        "is at or above our price, or approved: false if it's below or we don't have a price for the lane. "
        "Call this before create_load whenever a complete order includes a price. 'Same rate as last time' "
        "is not a quote."
    ),
    "strict": True,
    "input_schema": anthropic.transform_schema(QuoteCheck),
}

RAISE_QUOTE_FOR_APPROVAL_TOOL = {
    "name": "raise_quote_for_approval",
    "description": (
        "Send a complete order whose quote was not approved by approve_quote to the team on Slack, so a "
        "person can approve or reject the price. The shipper gets a reply saying their rate is under "
        "review. Pass the full order and the same quoted price. Do not call create_load for this order."
    ),
    "strict": True,
    "input_schema": anthropic.transform_schema(QuoteApprovalRequest),
}
