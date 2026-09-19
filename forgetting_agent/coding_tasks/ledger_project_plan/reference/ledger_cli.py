"""Stdlib CLI with explicit streams and a single owned database connection."""
import argparse
import json
import sqlite3
import sys
from contextlib import closing

import journal
import migration
import project
import reporting


def main(argv, stdin, stdout, stderr):
    parser = argparse.ArgumentParser(prog="ledger_cli")
    parser.add_argument("--database", required=True)
    parser.add_argument("command", choices=("migrate", "run", "history", "export"))
    args = parser.parse_args(argv)
    try:
        with closing(sqlite3.connect(args.database)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            if args.command == "migrate":
                result = {"version": migration.migrate(connection)}
            elif args.command == "run":
                envelope = project.decode(stdin.read())
                if (not isinstance(envelope, dict) or envelope.keys() != {"manifest", "sources"}
                        or not isinstance(envelope["manifest"], dict)
                        or not isinstance(envelope["sources"], dict)):
                    raise ValueError("expected manifest and sources envelope")
                result = project.run(connection, json.dumps(envelope["manifest"]),
                                     envelope["sources"])
            elif args.command == "history":
                result = journal.events(connection)
            else:
                text = reporting.export_csv(connection)
            if args.command != "export":
                text = json.dumps(result, ensure_ascii=False, sort_keys=True,
                                  separators=(",", ":")) + "\n"
    except (ValueError, sqlite3.Error) as error:
        stderr.write("error: " + " ".join(str(error).splitlines()) + "\n")
        return 2
    stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:], sys.stdin, sys.stdout, sys.stderr))
