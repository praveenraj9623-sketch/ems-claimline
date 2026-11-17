"""Command line: serve the app, place a phone call, rehearse in the terminal, or review a transcript."""

import argparse
import json
import logging
import sys
import urllib.error
import urllib.request

from .agent import Agent
from .config import Settings
from .groq import GroqClient
from .review import review_call


def cmd_serve(args) -> int:
    from .server import create_server

    settings = Settings.load()
    server, service = create_server(settings)
    url = f"http://127.0.0.1:{settings.port}"
    print(f"\nClaimLine is running at {url}")
    print(f"  AI reviewer : {'on (Groq)' if service.groq.enabled else 'off - set GROQ_API_KEY (transcript audit still runs)'}")
    if settings.phone_ready:
        print("  Phone calls : off - add to .env: " + ", ".join(settings.phone_ready))
    else:
        tunnel = service.public_base or service.detect_url()
        print(f"  Phone calls : ready for number ending {settings.call_to_number[-4:]}")
        print(f"  Public URL  : {tunnel or 'NOT FOUND - run: ngrok http %d' % settings.port}")
    print("Press Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
    return 0


def cmd_call(args) -> int:
    settings = Settings.load()
    request = urllib.request.Request(f"http://127.0.0.1:{settings.port}/api/phone/call", data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            print("Calling your phone. Pick up and talk to the agent. Call id:", json.loads(response.read())["id"])
            return 0
    except urllib.error.HTTPError as error:
        print("Could not place the call:", json.loads(error.read()).get("error", error.reason))
    except urllib.error.URLError:
        print(f"The app is not running. Start it first with: python -m claimline serve")
    return 1


def cmd_simulate(args) -> int:
    """Type the representative's lines yourself (or feed them from a file) and watch the agent respond."""
    agent = Agent()
    print("AGENT:", agent.start().text)
    lines = open(args.script, encoding="utf-8").read().splitlines() if args.script else None
    feed = iter(lines) if lines is not None else None
    while agent.phase != "ended":
        try:
            line = next(feed) if feed is not None else input("YOU (representative, blank = silence, /end = hang up): ")
        except (StopIteration, EOFError):
            break
        if line.strip() == "/end":
            break
        if feed is not None:
            print("YOU:", line)
        reply = agent.hear(line) if line.strip() else agent.silence()
        print("AGENT:", reply.text or ("(waiting quietly)" if reply.wait or reply.quiet else "(silent)"))
    return finish(agent.transcript, agent.result(), args.no_ai)


def finish(transcript, result, no_ai=False) -> int:
    settings = Settings.load()
    client = None if no_ai else GroqClient(settings.groq_api_key, settings.groq_review_model or settings.groq_model)
    scorecard, detail = review_call(transcript, result, client)
    print("\nclaim_result:")
    print(json.dumps(result, indent=2))
    print("\nscorecard:")
    print(json.dumps(scorecard, indent=2))
    print(f"\n(reviewed by: {detail.get('method')})")
    return 0


def cmd_review(args) -> int:
    transcript = json.load(open(args.transcript, encoding="utf-8"))
    result = json.load(open(args.result, encoding="utf-8")) if args.result else None
    return finish(transcript, result, args.no_ai)


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(prog="python -m claimline", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="start the dashboard and phone webhooks").set_defaults(run=cmd_serve)
    sub.add_parser("call", help="ring your phone (the app must be running)").set_defaults(run=cmd_call)
    sim = sub.add_parser("simulate", help="rehearse a call in the terminal")
    sim.add_argument("--script", help="text file with one representative line per row")
    sim.add_argument("--no-ai", action="store_true", help="skip the Groq reviewer")
    sim.set_defaults(run=cmd_simulate)
    rev = sub.add_parser("review", help="score a saved transcript.json")
    rev.add_argument("transcript")
    rev.add_argument("--result", help="claim_result.json to cross-check for invented data")
    rev.add_argument("--no-ai", action="store_true")
    rev.set_defaults(run=cmd_review)
    args = parser.parse_args(argv)
    return args.run(args)


if __name__ == "__main__":
    sys.exit(main())
