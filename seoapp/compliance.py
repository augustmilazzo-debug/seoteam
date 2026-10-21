"""Advertising-compliance screening for medical aesthetics content.

Two layers: fast rule checks (this module) and a Claude review (ai.compliance_review)
that quotes each problem and suggests compliant wording. This is automated
screening to catch common problems, not legal advice.
"""

import datetime as dt
import re

DISCLAIMER = ("Automated compliance screening based on common FTC, FDA and medical-board advertising "
              "guidance. It catches common problems; it is not legal advice.")

SEVERITY = {"must_fix": "Must fix", "should_fix": "Should fix", "note": "Check"}

# (id, severity, pattern, explanation, suggested approach)
CLAIM_RULES = [
    ("guarantee", "must_fix", r"\bguarantee[sd]?\b(?![^.]{0,40}\bnot\b)",
     "Results of medical treatments can't be guaranteed; guarantees are a classic misleading-advertising violation.",
     "Describe typical results and say results vary."),
    ("risk_free", "must_fix", r"\brisk[- ]free\b|\bno risks?\b|\bzero risk\b|\bno side[- ]effects?\b",
     "Every medical treatment carries risks; claiming none is false.",
     "Mention that side effects are usually mild and temporary, and are discussed at the consultation."),
    ("cure_miracle", "must_fix", r"\bcures?\b|\bcured\b|\bmiracle\b|\bmagic\b",
     "Cure and miracle claims are unsubstantiated health claims.",
     "Say the treatment can improve or reduce the appearance of the concern."),
    ("fda_certified", "must_fix", r"\bFDA[- ](certified|registered approval)\b",
     "The FDA doesn't certify treatments or clinics; the wording is false.",
     "Use 'FDA-approved' or 'FDA-cleared' only for the specific product and indication."),
    ("safest", "must_fix", r"\b(the )?safest\b|\b100% safe\b|\bcompletely safe\b|\bperfectly safe\b",
     "Absolute safety claims can't be substantiated.",
     "Say the treatment has a well-established safety profile when performed by a qualified provider."),
    ("superlative", "should_fix", r"#\s?1\b|\bnumber one\b|\bbest (medspa|med spa|injector|clinic|results|in\b)|\btop[- ]rated\b",
     "Superlatives like #1 or best need objective proof (an award or ranking you can cite).",
     "Cite the award or source, or use 'experienced' or 'highly rated (4.9 stars on Google)'."),
    ("painless", "should_fix", r"\bpainless\b|\bpain[- ]free\b|\bno pain\b",
     "Treatments involve some discomfort for many patients.",
     "Say 'most patients describe minimal discomfort' and mention numbing options."),
    ("permanent", "should_fix", r"(?<!not )(?<!isn't )\bpermanent\b|\blasts forever\b|\blifelong results\b",
     "Most aesthetic results are temporary; permanence is usually not substantiated.",
     "Give the typical duration range and note that it varies by person."),
    ("no_downtime", "should_fix", r"(?<!little or )(?<!little to )\bno downtime\b|\bzero downtime\b",
     "Some patients have swelling or bruising; 'no downtime' is often not accurate.",
     "Say 'little to no downtime for most patients'."),
    ("instant", "should_fix", r"\binstant(ly)? results?\b|\bimmediate,? permanent\b",
     "Results typically develop over days or weeks; instant-result claims mislead.",
     "Describe the real timeline (e.g. results appear in 3-14 days)."),
    ("free_offer", "should_fix", r"\bfree (botox|filler|treatment|units?|syringe|session)\b",
     "Free-treatment offers need clear terms (FTC) and may be restricted by your state's medical board.",
     "State the exact terms and conditions, or remove the offer."),
    ("fda_approved", "note", r"\bFDA[- ](approved|cleared)\b",
     "FDA approval applies to a specific product and indication; make sure the claim matches the label.",
     "Name the product and the approved use, e.g. 'Botox® Cosmetic is FDA-approved for frown lines'."),
    ("board_certified", "note", r"\bboard[- ]certified\b",
     "Many state boards require naming the specific certifying board.",
     "Write e.g. 'board-certified by the American Board of Dermatology'."),
    ("before_after", "note", r"\bbefore[- ]?(and|&|/)[- ]?after\b",
     "Before/after photos need written patient consent, no retouching, typical results and a 'results vary' note.",
     "Confirm consent is on file and add 'Individual results vary.' next to the photos."),
    ("testimonial", "note", r"\btestimonial|\b(\w+) (said|says|raved)\b|\"[^\"]{10,200}\"\s*[-–—]\s*[A-Z][a-z]+",
     "Patient testimonials must reflect typical results or be clearly labeled; patients need to consent to being quoted.",
     "Add 'Results vary' and confirm the patient's written consent."),
]

OFF_LABEL = re.compile(r"\b(botox|dysport|xeomin|daxxify|jeuveau|neurotoxin|tox)\b[^.]{0,80}\b(masseter|jaw slimming|"
                       r"lip flip|gummy smile|tmj|bruxism|teeth grinding|neck|tech neck|trap(ezius)?|shoulder|"
                       r"underarm sweat|scalp)\b", re.I)

# (brand as it should appear, pattern of the name, mark)
TRADEMARKS = [
    ("Botox® Cosmetic", r"botox", "®"), ("Dysport®", r"dysport", "®"), ("Xeomin®", r"xeomin", "®"),
    ("Daxxify®", r"daxxify", "®"), ("Jeuveau®", r"jeuveau", "®"), ("Juvéderm®", r"juv[eé]derm", "®"),
    ("Restylane®", r"restylane", "®"), ("RHA® Collection", r"\brha\b", "®"), ("Radiesse®", r"radiesse", "®"),
    ("Sculptra®", r"sculptra", "®"), ("Kybella®", r"kybella", "®"), ("CoolSculpting®", r"coolsculpting", "®"),
    ("HydraFacial®", r"hydra ?facial", "®"), ("Morpheus8", r"morpheus ?8", ""), ("Ultherapy®", r"ultherapy", "®"),
    ("Emsculpt®", r"emsculpt", "®"), ("SkinVive™", r"skinvive", "™"), ("Sofwave™", r"sofwave", "™"),
]

CHECKLIST = [
    ("No guaranteed results", "Never promise outcomes; describe typical results and say results vary."),
    ("No absolute safety or risk-free claims", "Every procedure has risks; mention they're discussed at consultation."),
    ("No cure / miracle / unsubstantiated health claims", "Use 'improves the appearance of' style wording."),
    ("Superlatives must be provable", "'#1', 'best', 'top-rated' only with a cited award, ranking or rating."),
    ("Accurate FDA language", "FDA approval is product + indication specific; never 'FDA-certified'."),
    ("Off-label uses are not presented as approved", "e.g. masseter or lip flip with neurotoxins: no implied FDA approval."),
    ("Correct trademarks", "Botox® Cosmetic, Juvéderm®, Restylane®, Sculptra® etc.: proper spelling, ® on first mention, "
                           "never as a generic word for all neurotoxins."),
    ("Before/after photos", "Written patient consent, unretouched, typical results, 'Individual results vary.'"),
    ("Testimonials and reviews", "Consent to be quoted, typical experience or clearly labeled, no incentives undisclosed."),
    ("Offers and pricing", "Clear terms, no bait pricing, follow the state's rules on discounts and free treatments."),
    ("Provider credentials", "Treatment pages name the licensed provider and their license type; 'board-certified' names the board."),
    ("Results vary + consultation", "Treatment content says results vary and a consultation determines candidacy."),
    ("Patient privacy (HIPAA)", "No patient names, photos or details without written authorization."),
]


def _sentence(text: str, start: int, end: int) -> str:
    a = max(text.rfind(".", 0, start), text.rfind("\n", 0, start)) + 1
    b_dot = text.find(".", end)
    b_nl = text.find("\n", end)
    b = min(x for x in (b_dot if b_dot >= 0 else len(text), b_nl if b_nl >= 0 else len(text)))
    return text[a:b + 1].strip()[:300]


def rule_check(text: str, kind: str = "post") -> list[dict]:
    """Instant rule-based findings: [{severity, rule, quote, why, fix, source}]."""
    text = text or ""
    issues, seen = [], set()
    for rid, sev, pat, why, fix in CLAIM_RULES:
        for m in re.finditer(pat, text, re.I):
            quote = _sentence(text, m.start(), m.end())
            if (rid, quote) in seen:
                continue
            seen.add((rid, quote))
            issues.append({"severity": sev, "rule": rid, "quote": quote, "why": why, "fix": fix, "source": "rules"})
            if len([i for i in issues if i["rule"] == rid]) >= 3:
                break
    for m in OFF_LABEL.finditer(text):
        quote = _sentence(text, m.start(), m.end())
        if ("off_label", quote) not in seen and not re.search(r"off[- ]label", quote, re.I):
            seen.add(("off_label", quote))
            issues.append({"severity": "should_fix", "rule": "off_label", "quote": quote,
                           "why": "This neurotoxin use is off-label. Providers may offer it, but ads must not imply FDA approval.",
                           "fix": "Add that this is an off-label use, discussed with your provider at consultation.",
                           "source": "rules"})
    for proper, pat, mark in TRADEMARKS:
        found = list(re.finditer(rf"\b{pat}", text, re.I))
        if not found:
            continue
        name = proper.split("®")[0].split("™")[0].split(" ")[0]
        wrong_case = [m for m in found if m.group(0)[:1].islower()]
        if wrong_case:
            issues.append({"severity": "should_fix", "rule": "trademark", "quote": _sentence(text, wrong_case[0].start(), wrong_case[0].end()),
                           "why": f"{name} is a registered brand name and must be capitalized (and never used as a generic word).",
                           "fix": f"Write {proper} on first mention and {name} after that.", "source": "rules"})
        elif mark and mark not in text[found[0].end():found[0].end() + 2]:
            issues.append({"severity": "note", "rule": "trademark", "quote": _sentence(text, found[0].start(), found[0].end()),
                           "why": f"Brand guidelines ask for {proper} on first mention.",
                           "fix": f"Change the first mention to {proper}.", "source": "rules"})
    if kind in ("post", "page") and re.search(r"\b(botox|filler|laser|injectable|treatment|procedure|neurotoxin)\b", text, re.I):
        if not re.search(r"results (may |can |will )?vary", text, re.I):
            issues.append({"severity": "should_fix", "rule": "results_vary", "quote": "",
                           "why": "Treatment content should state that individual results vary.",
                           "fix": "Add: 'Individual results vary. A consultation determines whether a treatment is right for you.'",
                           "source": "rules"})
        if not re.search(r"\bconsult", text, re.I):
            issues.append({"severity": "should_fix", "rule": "consultation", "quote": "",
                           "why": "Medical content should direct readers to a consultation rather than self-diagnosis.",
                           "fix": "Add a line inviting readers to book a consultation with a licensed provider.",
                           "source": "rules"})
    return issues


def merge(rule_issues: list[dict], ai_issues: list[dict]) -> list[dict]:
    """Combine both layers, dropping AI findings that quote the same sentence as a rule finding."""
    quotes = {i["quote"] for i in rule_issues if i["quote"]}
    out = list(rule_issues)
    for i in ai_issues:
        if i.get("quote") and any(i["quote"] in q or q in i["quote"] for q in quotes):
            continue
        out.append({**i, "source": "review"})
    order = {"must_fix": 0, "should_fix": 1, "note": 2}
    return sorted(out, key=lambda i: order.get(i["severity"], 3))


def status(issues: list[dict]) -> str:
    sev = {i["severity"] for i in issues if not i.get("resolved")}
    return "fail" if "must_fix" in sev else "review" if "should_fix" in sev else "pass"


def result(issues: list[dict], ai_checked: bool) -> dict:
    return {"status": status(issues), "issues": issues, "ai_checked": ai_checked,
            "checked_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M")}


def blocking(comp: dict | None) -> bool:
    """Must-fix issues remain and nobody approved an override."""
    comp = comp or {}
    return status(comp.get("issues", [])) == "fail" and not comp.get("override")


def apply_fix(text: str, issue: dict) -> tuple[str, bool]:
    """Replace the quoted sentence with the suggested rewrite when the review gave one."""
    quote, rewrite = issue.get("quote") or "", issue.get("rewrite") or ""
    if quote and rewrite and quote in text:
        return text.replace(quote, rewrite, 1), True
    return text, False
