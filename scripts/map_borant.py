"""Link existing users to the subjects Borant ID knows them by.

Run once, by hand, BEFORE switching AUTH_MODE to `gateway`, and read the report
before believing it:

    docker exec grantradar python scripts/map_borant.py --report
    docker exec grantradar python scripts/map_borant.py \
        --map giovanni.spitale@ibme.uzh.ch=01M0HJEE7EGDPK5TVDT34PYDE9

Why a script and not an automatic match at request time: linking by email is
defensible in principle, because the address arrives from the gate and not from
the client — but doing it live means one typo in the gate's admin panel
silently hands one person another person's account, in an app where one of the
two roles can spend on the institute's API key. A script gets read before it is
run, and prints what it did.

What this does NOT do, on purpose:

  * It never overwrites an existing link. A conflict is reported; --unlink
    undoes one deliberately.
  * It never touches password_hash. Local passwords stay populated in gateway
    mode, because that is what makes AUTH_MODE=local a way back — and a user
    who only ever arrived through the gate has no password to come back with.
    For those, --report says so and /admin can set one.
  * It never changes a role. Promotion to admin stays a human click, since
    admin is the role that can spend.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.db import get_db, init_db  # noqa: E402


def report(db) -> None:
    rows = db.execute(
        "SELECT id, username, email, role, active, borant_sub FROM users ORDER BY id"
    ).fetchall()
    linked = [r for r in rows if r["borant_sub"]]
    loose = [r for r in rows if not r["borant_sub"]]

    print(f"\n{len(rows)} utenti, {len(linked)} legati, {len(loose)} scoperti.\n")
    if linked:
        print("LEGATI - entrano dal gate:")
        for r in linked:
            print(f"  {r['username']:<14} {r['email']:<32} {r['role']:<7} {r['borant_sub']}")
    if loose:
        print("\nSCOPERTI - in gateway NON entrano finche' non hanno un subject:")
        for r in loose:
            flag = "" if r["active"] else "  (disattivato)"
            print(f"  {r['username']:<14} {r['email']:<32} {r['role']:<7}{flag}")
    print()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--map", action="append", default=[], metavar="EMAIL=SUBJECT",
                    help="lega un utente a un subject del gate; ripetibile")
    ap.add_argument("--unlink", action="append", default=[], metavar="EMAIL",
                    help="toglie il legame di un utente; ripetibile")
    ap.add_argument("--report", action="store_true",
                    help="stampa chi e' legato e chi no, e non cambia niente")
    args = ap.parse_args()

    init_db()
    changed = 0

    with get_db() as db:
        for pair in args.map:
            email, sep, subject = pair.partition("=")
            email, subject = email.strip().lower(), subject.strip()
            if not sep or not email or not subject:
                print(f"  SALTO     {pair!r}: serve la forma email=subject")
                continue
            user = db.execute("SELECT * FROM users WHERE lower(email) = ?", (email,)).fetchone()
            if user is None:
                print(f"  ASSENTE   {email}: nessun utente con questo indirizzo")
                continue
            if user["borant_sub"] == subject:
                print(f"  GIA-OK    {email} -> {subject}")
                continue
            if user["borant_sub"]:
                print(f"  CONFLITTO {email}: gia' legato a {user['borant_sub']}, non sovrascrivo. "
                      f"Usa --unlink prima, se e' voluto.")
                continue
            clash = db.execute("SELECT username FROM users WHERE borant_sub = ?",
                               (subject,)).fetchone()
            if clash is not None:
                print(f"  CONFLITTO {subject}: gia' usato da {clash['username']}, non tocco niente.")
                continue
            db.execute("UPDATE users SET borant_sub = ? WHERE id = ?", (subject, user["id"]))
            print(f"  LEGATO    {email} -> {subject}")
            changed += 1

        for email in args.unlink:
            email = email.strip().lower()
            user = db.execute("SELECT * FROM users WHERE lower(email) = ?", (email,)).fetchone()
            if user is None:
                print(f"  ASSENTE   {email}: nessun utente con questo indirizzo")
                continue
            if not user["borant_sub"]:
                print(f"  GIA-OK    {email}: non era legato a niente")
                continue
            db.execute("UPDATE users SET borant_sub = NULL WHERE id = ?", (user["id"],))
            print(f"  SLEGATO   {email} (era {user['borant_sub']})")
            changed += 1

        if args.map or args.unlink:
            print(f"\n{changed} righe cambiate.")
        report(db)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
