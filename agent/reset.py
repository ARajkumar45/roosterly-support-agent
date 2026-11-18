"""Re-open all practice tickets. This is the only file that calls the reset.

  python -m agent.reset

It asks you to type the word reset. It refuses outside practice mode, and it
refuses after the final run (when the file .final_run_done exists).
"""

import sys

from agent import config
from agent.helpdesk import Helpdesk, HelpdeskError
from agent.logs import archive
from agent.run import utc_now

FINAL_MARKER = config.ROOT / ".final_run_done"


def reset(settings, helpdesk, final_marker=FINAL_MARKER, say=print, ask_user=input,
          log_dir=config.ROOT / "logs"):
    if final_marker.exists():
        say("The final run is done (.final_run_done exists). Reset refused.")
        return 2
    try:
        status = helpdesk.status()
        if status.get("mode") != "practice":
            say(f"Mode is '{status.get('mode')}', not practice. Reset refused.")
            return 2
        say(f"Now: open {status.get('open_tickets')}, handled {status.get('handled_tickets')}.")
        if ask_user("This re-opens ALL practice tickets. Type reset to continue: ").strip() != "reset":
            say("Stopped. Nothing was changed.")
            return 2
        helpdesk.request("POST", "/practice/reset")
        status = helpdesk.status()
    except HelpdeskError as error:
        say(f"The helpdesk gave an error ({error}). Check GET /status before trying again.")
        return 1
    say(f"Reset done: open {status.get('open_tickets')}, handled {status.get('handled_tickets')}.")
    moved = archive(log_dir, "before_reset_" + utc_now().replace(":", ""))
    say(f"Moved {moved} old run log(s) aside. The next run starts with an empty log folder.")
    return 0


def main():
    settings = config.load()
    helpdesk = Helpdesk(settings.helpdesk_url, settings.helpdesk_key, settings.email, dry_run=False)
    return reset(settings, helpdesk)


if __name__ == "__main__":
    sys.exit(main())
