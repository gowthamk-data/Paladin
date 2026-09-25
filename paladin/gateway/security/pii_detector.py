import re
from typing import Optional


CREDIT_CARD_PATTERN = re.compile(r"\b\d(?:[ -]?\d){12,18}\b")
AADHAAR_PATTERN = re.compile(r"\b\d{4}\s?\d{4}\s?\d{4}\b")
EMAIL_PATTERN = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
PHONE_PATTERN = re.compile(r"\b\d{10}\b")
SSN_PATTERN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")

CREDIT_CARD_MASK = "****-****-****-****"
AADHAAR_MASK = "****-****-****"
EMAIL_MASK = "****@****.***"
PHONE_MASK = "**** ****"
SSN_MASK = "***-**-****"

PII_TYPES = {
    "CREDIT_CARD": (CREDIT_CARD_PATTERN, CREDIT_CARD_MASK),
    "AADHAAR": (AADHAAR_PATTERN, AADHAAR_MASK),
    "EMAIL": (EMAIL_PATTERN, EMAIL_MASK),
    "PHONE": (PHONE_PATTERN, PHONE_MASK),
    "SSN": (SSN_PATTERN, SSN_MASK),
}


def luhn_check(number: str) -> bool:
    digits = [int(d) for d in re.sub(r"\D", "", number)]
    if len(digits) < 13:
        return False
    total = 0
    reverse = digits[::-1]
    for i, d in enumerate(reverse):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


VERHOEFF_D = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
    [1, 2, 3, 4, 0, 6, 7, 8, 9, 5],
    [2, 3, 4, 0, 1, 7, 8, 9, 5, 6],
    [3, 4, 0, 1, 2, 8, 9, 5, 6, 7],
    [4, 0, 1, 2, 3, 9, 5, 6, 7, 8],
    [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
    [6, 5, 7, 8, 9, 1, 0, 4, 3, 2],
    [7, 6, 9, 5, 8, 2, 1, 0, 4, 3],
    [8, 7, 5, 6, 9, 3, 2, 1, 0, 4],
    [9, 8, 6, 7, 5, 4, 3, 2, 1, 0],
]

VERHOEFF_P = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
    [1, 5, 7, 6, 2, 8, 3, 0, 9, 4],
    [5, 8, 0, 3, 7, 9, 6, 1, 4, 2],
    [8, 0, 4, 7, 1, 5, 2, 9, 3, 6],
    [0, 9, 1, 8, 6, 4, 3, 2, 7, 5],
]


def verhoeff_check(number: str) -> bool:
    digits = [int(d) for d in re.sub(r"\D", "", number)]
    if len(digits) < 2:
        return False
    check = 0
    for i in range(len(digits) - 1, -1, -1):
        pos = (len(digits) - 1 - i) % 5
        d = digits[i]
        pi = VERHOEFF_P[pos][d]
        check = VERHOEFF_D[check][pi]
    return check == 0


def detect_pii(text: str):
    findings = []
    for pii_type, (pattern, mask) in PII_TYPES.items():
        for match in pattern.finditer(text):
            value = match.group()
            valid = True
            if pii_type == "CREDIT_CARD":
                valid = luhn_check(value.replace(" ", "").replace("-", ""))
            elif pii_type == "AADHAAR":
                valid = verhoeff_check(value)
            if valid:
                findings.append({"type": pii_type, "match": value, "mask": mask})
    return findings


def redact_pii(text: str) -> tuple:
    findings = detect_pii(text)
    redacted = text
    for finding in reversed(findings):
        redacted = redacted.replace(finding["match"], finding["mask"])
    return redacted, findings
