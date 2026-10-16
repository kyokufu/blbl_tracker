import argparse
import logging
import os
import time
import tomllib
import uuid

from .bilibili import Bilibili
from .http import GlobalError, SyncError, Transport
from .notion import Notion
from .sync import synchronize


def main(argv=None):
    parser = argparse.ArgumentParser(description="Bilibili → Notion; defaults to read-only dry-run")
    parser.add_argument("--config", default="config.local.toml")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true", help="write only after reviewing a successful dry-run")
    mode.add_argument("--check-schema", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        with open(args.config, "rb") as handle:
            config = tomllib.load(handle)
        token = os.environ.get("NOTION_TOKEN", "")
        source = str(uuid.UUID(os.environ.get("NOTION_DATA_SOURCE_ID", "")))
        if not token or token != token.strip() or any(c in token for c in "\r\n"):
            raise ValueError
        deadline = time.monotonic() + 240
        notion = Notion(Transport("api.notion.com", token=token, interval=0.4, deadline=deadline), source, config)
        if args.check_schema:
            notion.preflight()
            logging.info("schema mapping valid; no changes made")
            return 0
        return synchronize(notion, Bilibili(Transport("api.bilibili.com", deadline=deadline)), apply=args.apply)
    except (OSError, ValueError, TypeError):
        logging.error("configuration invalid: check TOML, NOTION_TOKEN and NOTION_DATA_SOURCE_ID")
        return 2
    except (GlobalError, SyncError) as error:
        logging.error("run stopped: %s", error)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
