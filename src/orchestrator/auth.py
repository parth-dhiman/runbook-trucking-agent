"""Auth orchestrator: create a Gmail token for an email account."""

from src.services.gmail import create_token, token_path


def main() -> None:
    email = ""
    while not email:
        email = input("Email to authorize: ").strip()

    try:
        create_token(email)
    except ValueError as err:
        print(err)
        return
    print(f"Token saved to {token_path(email)}")
