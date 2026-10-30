"""Email and Slack message templates for order intake."""

import datetime as dt

LOAD_CONFIRMATION = """\
Hey {first_name},

You're all set, your load is booked!

Load #: {load_id}
Your ref: {reference}
Pickup: {pickup_city}, {pickup_state} on {pickup_date}
Delivery: {delivery_city}, {delivery_state} on {delivery_date}
Equipment: {equipment}
{rate_line}

We'll keep you posted once the truck's rolling. Just reply here if anything changes.

Cheers,
Dispatch"""


MISSING_INFO = """\
Hey {first_name},

Thanks for sending this over! Before we can book it, we still need:

{missing_list}

Could you send us a new email with the full order, including the details above? We'll get it booked as soon as it comes in.

Cheers,
Dispatch"""

# How each missing field (keys from data/order_spec.json) is worded in the reply.
MISSING_FIELD_PROMPTS = {
    "customer_reference": "Your reference number (PO, order, or load #)",
    "pickup_location": "Pickup address (street, city, state, ZIP)",
    "pickup_date": "Pickup date",
    "delivery_location": "Delivery address (street, city, state, ZIP)",
    "delivery_date": "Delivery date",
    "commodity": "What's being shipped",
    "weight_lbs": "Total weight in lbs",
    "equipment_type": "Equipment type (dry van, reefer, flatbed, or step deck)",
    "temperature": "Reefer temperature (°F)",
}


def _first_name(contact_name: str | None) -> str:
    return contact_name.split()[0] if contact_name else "there"


def render_missing_info(missing_fields: list[str], contact_name: str | None) -> str:
    lines = [f"- {MISSING_FIELD_PROMPTS.get(f, f.replace('_', ' ').capitalize())}" for f in missing_fields]
    return MISSING_INFO.format(first_name=_first_name(contact_name), missing_list="\n".join(lines))


def _day(iso_date: str) -> str:
    d = dt.date.fromisoformat(iso_date)
    return f"{d:%a, %b} {d.day}"


def _equipment_text(order: dict) -> str:
    equipment = order["equipment_type"].capitalize()
    if order["temperature_f"] is not None:
        equipment += f" @ {order['temperature_f']:g}°F"
    return equipment


def render_load_confirmation(order: dict, load_id: str, price: float | None) -> str:
    """Fill the confirmation template. `order` is a LoadOrder dump."""
    pickup, delivery = order["pickup"], order["delivery"]
    return LOAD_CONFIRMATION.format(
        first_name=_first_name(order["contact_name"]),
        load_id=load_id,
        reference=order["customer_reference"],
        pickup_city=pickup["city"],
        pickup_state=pickup["state"],
        pickup_date=_day(pickup["date"]),
        delivery_city=delivery["city"],
        delivery_state=delivery["state"],
        delivery_date=_day(delivery["date"]),
        equipment=_equipment_text(order),
        rate_line=f"Rate: ${price:,.2f} all-in" if price is not None else "Rate: we'll confirm it with you shortly",
    )


QUOTE_UNDER_REVIEW = """\
Hey {first_name},

Thanks for the order! We're running your rate ({quote}) by our team and will get back to you shortly.

Your ref: {reference}
Pickup: {pickup_city}, {pickup_state} on {pickup_date}
Delivery: {delivery_city}, {delivery_state} on {delivery_date}

Cheers,
Dispatch"""


def render_quote_under_review(order: dict, quoted_price: float) -> str:
    """Holding reply to the shipper while their below-standard quote waits for approval."""
    pickup, delivery = order["pickup"], order["delivery"]
    return QUOTE_UNDER_REVIEW.format(
        first_name=_first_name(order["contact_name"]),
        quote=f"${quoted_price:,.2f}",
        reference=order["customer_reference"],
        pickup_city=pickup["city"],
        pickup_state=pickup["state"],
        pickup_date=_day(pickup["date"]),
        delivery_city=delivery["city"],
        delivery_state=delivery["state"],
        delivery_date=_day(delivery["date"]),
    )


def _stop_text(stop: dict) -> str:
    when = _day(stop["date"]) + (f", {stop['time_window']}" if stop["time_window"] else "")
    zip_code = f" {stop['zip_code']}" if stop["zip_code"] else ""
    return f"{stop['facility_name']}\n{stop['street_address']}, {stop['city']}, {stop['state']}{zip_code}\n{when}"


def render_quote_approval(order: dict, evaluation: dict, source: dict | None) -> tuple[str, list[dict]]:
    """Slack fallback text and Block Kit blocks asking a person to approve a below-standard quote.

    `order` is a LoadOrder dump; `evaluation` comes from the quote check (lane,
    quoted_price, standard_price, and difference when we have a price for the lane).
    """
    pickup, delivery = order["pickup"], order["delivery"]
    route = f"{pickup['city']}, {pickup['state']} → {delivery['city']}, {delivery['state']}"
    quoted, standard = evaluation["quoted_price"], evaluation["standard_price"]

    if standard is not None:
        below = -evaluation["difference"]
        summary = (
            f"*{order['customer_name']}* offered *${quoted:,.2f}* for *{route}*.\n"
            f"Our standard price is *${standard:,.2f}*, so this is *${below:,.2f} ({below / standard:.0%}) below*."
        )
        fallback = f"Quote approval needed: {order['customer_name']} offered ${quoted:,.2f} on {route} (standard ${standard:,.2f})"
    else:
        summary = (
            f"*{order['customer_name']}* offered *${quoted:,.2f}* for *{route}*.\n"
            f"We don't have a standard price for this lane, so it needs a person to price it."
        )
        fallback = f"Quote approval needed: {order['customer_name']} offered ${quoted:,.2f} on {route} (no standard price)"

    equipment = _equipment_text(order)
    freight = f"{order['commodity']}\n{order['weight_lbs']:,.0f} lbs" + (
        f" · {order['handling_units']}" if order["handling_units"] else ""
    )
    contact = " · ".join(p for p in (order["contact_name"], order["contact_phone"]) if p) or "Not given"

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "Quote needs approval", "emoji": True}},
        {"type": "section", "text": {"type": "mrkdwn", "text": summary}},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Customer ref*\n{order['customer_reference']}"},
                {"type": "mrkdwn", "text": f"*Contact*\n{contact}"},
                {"type": "mrkdwn", "text": f"*Pickup*\n{_stop_text(pickup)}"},
                {"type": "mrkdwn", "text": f"*Delivery*\n{_stop_text(delivery)}"},
                {"type": "mrkdwn", "text": f"*Equipment*\n{equipment}"},
                {"type": "mrkdwn", "text": f"*Freight*\n{freight}"},
                {"type": "mrkdwn", "text": f"*Lane*\n{evaluation['lane']}"},
            ],
        },
    ]
    if order["special_instructions"]:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Notes*\n{order['special_instructions']}"}})
    if source:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"From {source['from']} · \"{source['subject']}\""}]})
    blocks.append(
        {"type": "section", "text": {"type": "mrkdwn", "text": "*Approve this price?* Reply in this thread with :white_check_mark: or :x:."}}
    )
    return fallback, blocks
