#!/usr/bin/env python3
"""Generate a FICTIONAL corpus of Israeli-style court decisions for end-to-end privacy tests.

Every person, number, address and case below is invented (deterministic seed). The documents
copy the *shape* of real Israeli verdicts — court header, case numbers, parties with national
IDs, addresses, phones, e-mails, company numbers, land parcels, bank details, vehicle plates,
dates of birth, lawyers, judge — so HermesCloak can be tested on realistic text without
touching any real person's data.

Outputs (next to this script):
  cases/case_NN.md   the documents the sandbox agent reads with its file tools
  manifest.json      every identifying value: type, case, and the expectation:
                       "masked" → HermesCloak must never let it reach the model
                       "gap"    → known limitation (a first name on its own; a name missing from
                                  the client list when NER is off): leaks are MEASURED, not failures
  gazetteer.txt      the client list a firm would load (most party names; a few deliberately
                     left out to measure the no-NER gap)

  python tests/fixtures/verdicts/generate.py
"""
import json
import os
import random

HERE = os.path.dirname(os.path.abspath(__file__))
RNG = random.Random(20261002)

FIRST = ["אורית", "יואב", "מיכל", "רונן", "שירן", "עמית", "טליה", "גלעד", "נועה", "איתי",
         "הדס", "אסף", "ליאת", "דורון", "ענבל", "ניר"]
LAST = ["בן־שושן", "אלמוג", "קרמר", "חדד", "וקנין", "רוזנטל", "אביטל", "שגיא", "פלד",
        "מזרחי־לוי", "גרינברג", "סויסה", "נחמיאס", "ברקוביץ"]
STREETS = ["רחוב הדקל", "שדרות הנרקיסים", "רחוב האלון", "רחוב המעיין", "רחוב הגפן", "דרך השקד"]
CITIES = ["רמת השרון", "חדרה", "באר שבע", "קריית מוצקין", "מודיעין", "נתניה", "אשדוד"]
COURTS = ["בית משפט השלום בתל אביב–יפו", "בית המשפט המחוזי בחיפה", "בית משפט השלום בבאר שבע",
          "בית המשפט המחוזי מרכז–לוד", "בית משפט השלום בנתניה", "בית המשפט לענייני משפחה בחדרה"]
JUDGES = ["כב' השופטת דפנה אלקלעי", "כב' השופט אברהם צור", "כב' השופטת רינת שמעוני",
          "כב' השופט יהודה נבון"]
LAWYERS = ["עו\"ד שמואל ורד", "עו\"ד הילה ברנע", "עו\"ד מאיר אוחנה", "עו\"ד רחל זילבר"]


def _check_digit_ok(nine: str) -> bool:
    total = 0
    for i, ch in enumerate(nine):
        x = int(ch) * (1 if i % 2 == 0 else 2)
        total += x if x < 10 else x - 9
    return total % 10 == 0


def israeli_id(prefix: str = "") -> str:
    while True:
        body = prefix + "".join(RNG.choice("0123456789") for _ in range(8 - len(prefix)))
        for d in "0123456789":
            if _check_digit_ok(body + d):
                return body + d


def luhn_card() -> str:
    digits = [4, 5, 8, 0] + [RNG.randrange(10) for _ in range(11)]
    for d in range(10):
        cand = digits + [d]
        total, parity = 0, len(cand) % 2
        for i, x in enumerate(cand):
            if i % 2 == parity:
                x *= 2
                x = x - 9 if x > 9 else x
            total += x
        if total % 10 == 0:
            s = "".join(map(str, cand))
            return " ".join(s[i:i + 4] for i in range(0, 16, 4))


def phone() -> str:
    return f"05{RNG.choice('02348')}-{RNG.randrange(1000000, 9999999)}"


def person():
    return f"{RNG.choice(FIRST)} {RNG.choice(LAST)}"


def build():
    cases, manifest, gazetteer = [], [], []
    templates = [
        ("ת\"א", "תביעה כספית — הפרת הסכם שכירות", "civil"),
        ("ת\"פ", "פסק דין — עבירות מרמה והונאה בכרטיסי אשראי", "criminal"),
        ("ה\"פ", "המרצת פתיחה — פירוק שיתוף במקרקעין", "land"),
        ("תיק", "החלטה — בקשה לביטול עיקול חשבון בנק", "enforcement"),
        ("פש\"ר", "החלטה — בקשה להכרזה על פשיטת רגל", "insolvency"),
        ("תמ\"ש", "פסק דין — מזונות ומשמורת (פורסם בהשמטת פרטים מזהים — כאן בדוי)", "family"),
    ]
    used_names = set()
    for n, (prefix, title, kind) in enumerate(templates, 1):
        while True:
            a, b = person(), person()
            if a != b and a not in used_names and b not in used_names:
                used_names |= {a, b}
                break
        num = f"{RNG.randrange(10000, 99999)}-{RNG.randrange(1, 12):02d}-{RNG.choice(['22', '23', '24'])}"
        id_a, id_b = israeli_id(), israeli_id()
        ph_a, ph_b = phone(), phone()
        mail_a = f"{a.split()[0][::-1][:3].encode('utf-8').hex()[:6]}.client{n}@example.org"
        addr_a = f"{RNG.choice(STREETS)} {RNG.randrange(2, 90)}, {RNG.choice(CITIES)}"
        addr_b = f"{RNG.choice(STREETS)} {RNG.randrange(2, 90)}, {RNG.choice(CITIES)}"
        company = israeli_id("51")
        acct = str(RNG.randrange(100000, 999999))
        bank = f"בנק {RNG.choice(['לאומי', 'הפועלים', 'דיסקונט', 'מזרחי טפחות'])}, סניף {RNG.randrange(100, 999)}, " \
               f"חשבון {acct}"
        plate = f"{RNG.randrange(100, 999)}-{RNG.randrange(10, 99)}-{RNG.randrange(100, 999)}"
        dob = f"{RNG.randrange(1, 28):02d}.{RNG.randrange(1, 12):02d}.{RNG.randrange(1955, 2001)}"
        gush = f"גוש {RNG.randrange(6000, 7999)} חלקה {RNG.randrange(10, 400)}"
        card = luhn_card()
        court, judge, lawyer = RNG.choice(COURTS), RNG.choice(JUDGES), RNG.choice(LAWYERS)
        in_gaz_b = n not in (2, 5)          # cases 2 and 5: second party NOT in the client list
        gazetteer.append((a, "לקוח"))
        if in_gaz_b:
            gazetteer.append((b, "לקוח"))

        body = f"""> **מסמך בדוי לחלוטין לצורכי בדיקה (HermesCloak test fixture). כל השמות, המספרים והפרטים מומצאים.**

# {court}
## {prefix} {num} — {title}

**לפני:** {judge}

**התובע/המבקש:** {a}, ת"ז {id_a}
מען: {addr_a} · טלפון: {ph_a} · דוא"ל: {mail_a}
ב"כ: {lawyer}

**נגד**

**הנתבע/המשיב:** {b}, ת.ז. {id_b}, יליד {dob}
מען: {addr_b} · טלפון נייד {ph_b}
החברה המשותפת: "{a.split()[1]} השקעות בע"מ", ח.פ {company}

### רקע עובדתי
1. בין הצדדים נכרת הסכם ביום {RNG.randrange(1, 28)}.{RNG.randrange(1, 12)}.2021 בנוגע לנכס הידוע כ{gush}, ברחוב {addr_a.split(',')[0].replace('רחוב ', '')}.
2. לטענת {a.split()[0]}, הנתבע {b} לא העביר את התשלומים לחשבון ב{bank}, ובמקום זאת חויב כרטיס האשראי {card}.
3. רכב הנתבע, מספר רישוי {plate}, עוקל במסגרת תיק ההוצאה לפועל.
4. בדיון שהתקיים, העיד {b} כי ניתן להשיגו בטלפון {ph_b} וכי כתובתו עודכנה ל{addr_b}.

### דיון והכרעה
5. לאחר שעיינתי בטענות הצדדים, אני מקבל את התביעה בחלקה. על הנתבע {b} (ת"ז {id_b}) לשלם לתובע {a} סך של {RNG.randrange(12, 480)},000 ש"ח.
6. המזכירות תשלח את פסק הדין לב"כ הצדדים ולתובע בדוא"ל {mail_a}.

ניתן היום, בהעדר הצדדים.
{judge.replace("כב' ", "")}
"""
        path = f"cases/case_{n:02d}.md"
        cases.append((path, body))

        def add(value, typ, expected):
            manifest.append({"case": n, "type": typ, "value": value, "expected": expected})

        add(a, "name (client list)", "masked")
        add(b, "name (client list)" if in_gaz_b else "name (NOT in client list)", "masked" if in_gaz_b else "gap")
        add(id_a, "national ID", "masked")
        add(id_b, "national ID", "masked")
        add(ph_a, "phone", "masked")
        add(ph_b, "phone", "masked")
        add(mail_a, "email", "masked")
        add(company, "company number", "masked")
        add(card, "credit card", "masked")
        add(gush, "land parcel", "masked")
        add(addr_a, "street address", "masked")
        add(addr_b, "street address", "masked")
        add(acct, "bank account", "masked")
        add(plate, "vehicle plate", "masked")
        add(dob, "date of birth", "masked")
        add(num, "case number", "masked")
        add(a.split()[0], "first name alone", "gap")
    return cases, manifest, gazetteer


def main():
    cases, manifest, gazetteer = build()
    os.makedirs(os.path.join(HERE, "cases"), exist_ok=True)
    index = ["# תיקים בדויים לבדיקה (fictional test cases)", ""]
    # cases/AGENTS.md (the sandbox agent's instructions) is hand-written and kept as is
    for path, body in cases:
        with open(os.path.join(HERE, path), "w", encoding="utf-8") as f:
            f.write(body)
        title = body.split("\n## ", 1)[1].split("\n", 1)[0]
        index.append(f"- `{os.path.basename(path)}` — {title}")
    with open(os.path.join(HERE, "cases", "README.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(index) + "\n")
    with open(os.path.join(HERE, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    with open(os.path.join(HERE, "gazetteer.txt"), "w", encoding="utf-8") as f:
        f.write("".join(f"{s}\t{t}\n" for s, t in gazetteer))
    print(f"{len(cases)} cases, {len(manifest)} identifying values "
          f"({sum(m['expected'] == 'masked' for m in manifest)} must be masked), "
          f"{len(gazetteer)} gazetteer names")


if __name__ == "__main__":
    main()
