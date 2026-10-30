from dotenv import load_dotenv

from src.config.params import MODE

load_dotenv()

if __name__ == "__main__":
    if MODE == "auth":
        from src.orchestrator.auth import main as run_auth

        run_auth()
    elif MODE == "inbox":
        from src.orchestrator.inbox import main as run_inbox

        run_inbox()
