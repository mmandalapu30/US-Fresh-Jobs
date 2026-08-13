"""JobClassifier — derive a role category and a seniority level from a job title.

Why title-driven: measured coverage on 182,766 real jobs.

| field | coverage | usable? |
|---|---|---|
| `title` | 100% | yes — the only reliable signal |
| `department` | 37.6% | secondary hint only, and free-text ("Store", "Telehealth") |
| `seniority` | 5.0% | no — and inconsistent ("Entry", "Entry Level", "1-3 years") |

The taxonomy is built from the actual title distribution, not a generic template. The top
terms in this dataset are *manager, assistant, technician, associate, specialist, sales,
engineer, nurse, driver, teacher* — a broad labour-market mix. A software-centric taxonomy
would leave the large majority uncategorised.

Category and level are **orthogonal**. "Sales Manager" is category=sales, level=manager;
collapsing them would make it impossible to ask for "any management role" or "all sales
roles" independently.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

__all__ = ["CATEGORIES", "SENIORITY_LEVELS", "Classification", "JobClassifier"]


@dataclass(frozen=True, slots=True)
class Category:
    slug: str
    name: str
    #: Emoji used by the UI. Kept with the data so the frontend needs no lookup table.
    icon: str


#: Ordered by specificity, NOT by size. The first matching rule wins, so narrow
#: categories (healthcare, education) must precede broad ones (admin, management).
CATEGORIES: Final[tuple[Category, ...]] = (
    Category("healthcare", "Healthcare & Nursing", "🩺"),
    Category("education", "Education & Training", "🎓"),
    Category("data-engineering", "Data Engineering", "🔧"),
    Category("data-analytics", "Data Analytics", "📊"),
    Category("data-science", "Data Science & ML", "🧪"),
    Category("software", "Software Engineering", "💻"),
    Category("it-ops", "IT & Infrastructure", "🖥️"),
    Category("engineering", "Engineering & Technical", "⚙️"),
    Category("science", "Science & Research", "🔬"),
    Category("skilled-trades", "Skilled Trades & Maintenance", "🔧"),
    Category("manufacturing", "Manufacturing & Production", "🏭"),
    Category("construction", "Construction & Facilities", "🏗️"),
    Category("transport", "Transportation & Logistics", "🚚"),
    Category("food-hospitality", "Food Service & Hospitality", "🍽️"),
    Category("retail-customer", "Retail & Customer Service", "🛍️"),
    Category("sales", "Sales & Business Development", "📈"),
    Category("marketing", "Marketing & Communications", "📣"),
    Category("finance", "Finance & Accounting", "💰"),
    Category("legal", "Legal & Compliance", "⚖️"),
    Category("hr", "Human Resources", "👥"),
    Category("social-services", "Social Services & Nonprofit", "🤝"),
    Category("security", "Security & Protective Services", "🛡️"),
    Category("admin", "Administrative & Office", "🗂️"),
    Category("management", "Management & Operations", "📋"),
    Category("other", "Other", "•"),
)

CATEGORY_BY_SLUG: Final[dict[str, Category]] = {c.slug: c for c in CATEGORIES}

#: (slug, pattern) in priority order. Patterns are matched against a lowercased title.
#:
#: Patterns carry a LEADING word boundary only, so a stem matches its inflections
#: ("manufactur" -> "Manufacturing", "biolog" -> "Biologist"). A trailing  would block
#: exactly that, which silently sent thousands of jobs to "Other" on the first attempt.
#: Alternatives that must not match forward carry their own trailing boundary.
_RULES: Final[tuple[tuple[str, str], ...]] = (
    # --- healthcare -------------------------------------------------------------
    # First because clinical titles collide with everything: "Clinical Director" is
    # healthcare, not management; "Patient Access Representative" is not customer service.
    (
        "healthcare",
        r"\b(?:nurse|nursing|rn|lpn|lvn|cna|np|crna|physician|doctor|surgeon|dentist|"
        r"dental|hygienist|pharmacist|pharmacy|therapist|therapy|physical therap|"
        r"occupational therap|respiratory|radiolog|sonograph|ultrasound|phlebotom|"
        r"medical|clinical|clinician|patient|caregiver|care giver|home health|"
        r"hospice|paramedic|emt|veterinar|optometr|chiropract|psychiatr|psycholog|"
        r"behavioral health|mental health|midwife|anesthesi|cardiolog|oncolog|"
        r"pediatric|geriatric|telehealth|healthcare|health care|patient care|"
        r"certified nursing|direct support professional|dsp|medication aide|"
        # ABA roles read as "technician" and were being filed under skilled trades.
        r"behavior technician|behavior analyst|\brbt\b|\bbcba\b|applied behavior)",
    ),
    # --- education --------------------------------------------------------------
    (
        "education",
        r"\b(?:teacher|teaching|educator|professor|lecturer|instructor|tutor|"
        r"substitute teacher|paraprofessional|school principal|assistant principal|dean|curriculum|"
        r"childcare|child care|preschool|elementary|kindergarten|daycare|"
        r"school counselor|academic|admissions|registrar|librarian|"
        r"early childhood|special education|coach)",
    ),
    # --- software & IT ----------------------------------------------------------
    (
        # --- data & analytics -------------------------------------------------
        # Data engineering: building and running the pipelines and stores.
        # Checked before analytics because "Analytics Engineer" builds pipelines and
        # belongs here, not with the people querying them.
        "data-engineering",
        r"\b(?:data engineer|analytics engineer|data architect|data warehouse|"
        r"data platform|data pipeline|data infrastructure|data ops|dataops|"
        r"\betl\b|\belt\b|big data|databricks|snowflake|hadoop|kafka|dbt\b|"
        # Bare "spark" caught "SPARK AmeriCorps Member" — a volunteer programme, not
        # Apache Spark. Tool names that are also ordinary words need qualifying.
        r"apache spark|pyspark|spark sql|"
        r"data modell?er|data steward|data quality|data governance|"
        # "Database Administrator" is infrastructure work; "Database Developer" is not.
        r"database(?! administrator) (?:developer|engineer))",
    ),
    # --- data analytics ---------------------------------------------------------
    (
        "data-analytics",
        r"\b(?:data analy|business intelligence|\bbi\b(?! ?directional)|"
        r"reporting analyst|insights analyst|analytics analyst|business analyst|"
        r"analytics manager|analytics lead|data analytics|"
        r"tableau|power bi|qlik|looker|"
        # Bare "analytics" last so the more specific rules above win first.
        r"analytics)",
    ),
    # --- data science / ML ------------------------------------------------------
    (
        "data-science",
        r"\b(?:data scien|machine learning|\bml\b|artificial intelligence|\bai\b|"
        r"deep learning|\bnlp\b|computer vision|applied scien|research scien|"
        r"quantitative|biostatis|statistician|decision scien|\bmlops\b)",
    ),
    # --- software engineering ---------------------------------------------------
    (
        "manufacturing",
        # A "Programmer" is not always a software one. CNC, laser, punch-press and PLC
        # programmers write toolpaths and ladder logic, not code -- "Laser and CNC Punch
        # Machine Programmer" was landing on the software board. This sits before
        # `software` so the bare `programmer` alternative there cannot claim them. Both a
        # machine word AND "programmer" are required, so "Software Engineer, Laser
        # Systems" is untouched.
        r"\b(?:cnc|laser|punch|press brake|machine tool|lathe|edm|plc)\b[^,]{0,40}"
        r"\bprogramm(?:er|ing)\b"
        r"|\bprogramm(?:er|ing)\b[^,]{0,40}"
        r"\b(?:cnc|laser|punch|press brake|machine tool|lathe)\b",
    ),
    (
        "software",
        r"\b(?:software|developer|programmer|full.?stack|"
        # Bare "front end" is a supermarket department before it is a discipline:
        # "Front End - Cashier" was landing here. Require the role noun. The one-word
        # "frontend"/"backend" spellings are unambiguous and stay bare.
        r"front.?end (?:dev|engineer)|back.?end (?:dev|engineer)|"
        r"frontend|backend|devops|\bsre\b|site reliability|platform engineer|"
        r"mobile developer|\bios\b|android|web developer|api engineer|"
        r"qa engineer|test engineer|automation engineer|embedded|firmware|"
        r"game developer|solutions architect|technical architect|cloud engineer|"
        # "Systems Engineer" and "Applications Engineer" are NOT reliably software: in
        # aerospace/defence a systems engineer does requirements and integration, and
        # "Thermal and Fluid Systems Engineer" was landing here. They fall through to
        # `engineering`, which is the honest answer for an ambiguous title.
        r"kubernetes|integration engineer)",
    ),
    # --- IT and infrastructure --------------------------------------------------
    (
        "it-ops",
        r"\b(?:it support|help ?desk|desktop support|technical support|"
        r"system administrator|systems administrator|network administrator|"
        r"network engineer|sysadmin|database administrator|\bdba\b|"
        r"infrastructure|cyber ?security|information security|infosec|"
        r"salesforce|\bsap\b|\berp\b|it manager|it director|"
        r"service desk|it operations|\bnoc\b|\bsoc\b)",
    ),
    # --- engineering (non-software) ---------------------------------------------
    (
        "engineering",
        r"\b(?:engineer|engineering|mechanical|electrical|civil|chemical|structural|"
        r"aerospace|industrial engineer|process engineer|design engineer|"
        r"drafter|cad|surveyor|architect|geolog|environmental engineer)",
    ),
    # --- science ----------------------------------------------------------------
    (
        "science",
        r"\b(?:scientist|research|laboratory|\blab\b|chemist|biolog|microbiolog|"
        r"clinical research|toxicolog|epidemiolog|statistician|bioinformatic)",
    ),
    # --- skilled trades ---------------------------------------------------------
    (
        "skilled-trades",
        r"\b(?:technician|mechanic|electrician|plumber|hvac|welder|welding|"
        r"machinist|maintenance|millwright|pipefitter|steamfitter|boilermaker|"
        r"locksmith|automotive|diesel|repair|installer|service tech|field service|"
        r"lineman|apprentice|journeyman|refrigeration|elevator)",
    ),
    # --- manufacturing ----------------------------------------------------------
    (
        "manufacturing",
        r"\b(?:production|manufactur|assembler|assembly|machine operator|"
        r"forklift|packag|fabricat|quality control|quality assurance|\bqc\b|"
        r"press operator|cnc|extrusion|molding|plant operator|line operator|"
        r"sanitation|processor)",
    ),
    # --- construction -----------------------------------------------------------
    (
        "construction",
        r"\b(?:construction|carpenter|mason|roofer|painter|concrete|framer|"
        r"heavy equipment|excavat|landscap|groundskeep|janitor|custodian|"
        r"housekeep|facilities|building engineer|superintendent|laborer|"
        r"general labor|utility worker|roadway|paving)",
    ),
    # --- transport & logistics --------------------------------------------------
    (
        "transport",
        r"\b(?:driver|\bcdl\b|trucking|truck|delivery|courier|chauffeur|"
        r"warehouse|forklift operator|logistics|supply chain|shipping|receiving|"
        r"dispatcher|freight|fleet|transportation|material handler|picker|packer|"
        r"loader|route|transit|bus operator|conductor|flight|"
        # "Pilot Program" / "Pilot Project" are business terms, not aviation.
        r"pilot(?! program| project| study| phase|ing))",
    ),
    # --- food & hospitality -----------------------------------------------------
    (
        "food-hospitality",
        r"\b(?:cook|chef|kitchen|culinary|baker|barista|bartender|server|waiter|"
        r"waitress|dishwasher|busser|host|hostess|restaurant|food service|"
        r"food prep|line cook|prep cook|catering|hotel|hospitality|"
        r"front desk|concierge|banquet|steward|crew member)",
    ),
    # --- retail & customer service ----------------------------------------------
    (
        "retail-customer",
        r"\b(?:cashier|retail|store associate|sales associate|customer service|"
        r"customer care|customer support|customer success|call center|"
        r"contact center|client service|guest service|teller|stocker|"
        r"merchandiser|merchandising|team member|store\b|shift lead|"
        r"key holder|keyholder|checkout|stock clerk|store clerk)",
    ),
    # --- sales ------------------------------------------------------------------
    (
        "sales",
        r"\b(?:sales|account executive|account manager|business development|"
        r"territory|inside sales|outside sales|sales rep|\bbdr\b|\bsdr\b|"
        r"financial advisor|insurance agent|real estate|broker|leasing|"
        r"membership advisor|enrollment)",
    ),
    # --- marketing --------------------------------------------------------------
    (
        "marketing",
        r"\b(?:marketing|brand|content|social media|seo|sem|communications|"
        r"public relations|\bpr\b|copywriter|creative|graphic design|designer|"
        r"ux|ui|product design|videograph|photograph|editor|journalist)",
    ),
    # --- finance ----------------------------------------------------------------
    (
        "finance",
        r"\b(?:account(?:ing|ant)|bookkeep|payroll|finance|financial analyst|"
        r"controller|treasur|audit|tax|billing|accounts payable|accounts receivable|"
        r"\bap\b|\bar\b|credit|collections|underwrit|actuar|budget|revenue cycle|"
        r"claims|banking|mortgage|loan)",
    ),
    # --- legal ------------------------------------------------------------------
    (
        "legal",
        r"\b(?:attorney|lawyer|legal|paralegal|counsel|compliance|regulatory|"
        r"contracts|litigation|privacy|risk manage)",
    ),
    # --- HR ---------------------------------------------------------------------
    (
        "hr",
        r"\b(?:human resources|\bhr\b|recruit|talent acquisition|talent|"
        r"people operations|staffing|onboarding|benefits|compensation|"
        r"employee relations|training and development|learning and development)",
    ),
    # --- social services & nonprofit --------------------------------------------
    (
        "social-services",
        r"\b(?:social worker|social work|case manager|case work|counselor|counseling|"
        r"volunteer|nonprofit|non.?profit|community|outreach|advocacy|advocate|"
        r"chaplain|ministry|pastor|board member|development officer|"
        r"program coordinator|youth)",
    ),
    # --- security ---------------------------------------------------------------
    (
        "security",
        r"\b(?:security|guard|patrol officer|surveillance|loss prevention|"
        r"police officer|firefighter|correctional officer|security officer|peace officer|loss prevention officer|safety)",
    ),
    # --- administrative ---------------------------------------------------------
    (
        "admin",
        r"\b(?:administrative|admin assistant|executive assistant|receptionist|"
        r"secretary|office manager|office assistant|data entry|scheduler|"
        r"coordinator|clerical|records|front office)",
    ),
    # --- management (last: nearly every title can contain "manager") -------------
    (
        "management",
        r"\b(?:manager|director|supervisor|president|chief|\bceo\b|\bcfo\b|\bcto\b|"
        r"\bcoo\b|executive|head of|general manager|operations|project manager|"
        r"program manager|product manager|leadership|principal)",
    ),
)

_COMPILED: Final[tuple[tuple[str, re.Pattern[str]], ...]] = tuple(
    (slug, re.compile(pattern, re.IGNORECASE)) for slug, pattern in _RULES
)


#: Seniority levels, ordered from junior to senior. Stored as a string so the API and UI
#: share one vocabulary.
SENIORITY_LEVELS: Final[tuple[str, ...]] = (
    "INTERNSHIP",
    "ENTRY",
    "MID",
    "SENIOR",
    "LEAD",
    "MANAGER",
    "DIRECTOR",
    "EXECUTIVE",
    "UNKNOWN",
)

#: Ordered most-senior first. An "Executive Director" is EXECUTIVE, not DIRECTOR, and
#: "Senior Director" is DIRECTOR, not SENIOR -- so the strongest signal must win.
_LEVEL_RULES: Final[tuple[tuple[str, str], ...]] = (
    (
        "EXECUTIVE",
        r"\b(?:chief|\bceo\b|\bcfo\b|\bcto\b|\bcoo\b|\bcio\b|\bciso\b|"
        r"president|executive director|\bevp\b|\bsvp\b|\bvp\b|"
        r"vice president|partner|owner|founder)\b",
    ),
    ("DIRECTOR", r"\b(?:director|head of|dean|principal(?! engineer| scientist| consultant))\b"),
    (
        "MANAGER",
        r"\b(?:manager|supervisor|superintendent|foreman|"
        r"general manager|store manager|\bgm\b)\b",
    ),
    (
        "LEAD",
        r"\b(?:lead|leader|team lead|shift lead|crew lead|"
        r"principal engineer|principal scientist|staff engineer|architect)\b",
    ),
    (
        "SENIOR",
        r"\b(?:senior|\bsr\.?\b|experienced|advanced|expert|"
        r"\biii\b|\biv\b|level 3|level 4)\b",
    ),
    (
        "INTERNSHIP",
        r"\b(?:intern|internship|co.?op|trainee|apprentice|"
        r"student|fellow(?:ship)?|residency|resident)\b",
    ),
    (
        "ENTRY",
        r"\b(?:entry|junior|\bjr\.?\b|assistant|associate|"
        r"\bi\b|level 1|new grad|graduate|no experience|"
        r"helper|aide|attendant|trainee)\b",
    ),
)

_LEVEL_COMPILED: Final[tuple[tuple[str, re.Pattern[str]], ...]] = tuple(
    (level, re.compile(pattern, re.IGNORECASE)) for level, pattern in _LEVEL_RULES
)

#: Source-provided seniority strings, normalised. Only 5% of rows have one, but where it
#: exists it is more authoritative than a title guess.
_SOURCE_SENIORITY: Final[dict[str, str]] = {
    "entry level": "ENTRY",
    "entry": "ENTRY",
    "junior": "ENTRY",
    "associate": "ENTRY",
    "0-1 year": "ENTRY",
    "1-3 years": "MID",
    "mid-level": "MID",
    "mid level": "MID",
    "intermediate": "MID",
    "experienced": "SENIOR",
    "mid-senior level": "SENIOR",
    "senior": "SENIOR",
    "senior level": "SENIOR",
    "lead": "LEAD",
    "management": "MANAGER",
    "manager": "MANAGER",
    "director": "DIRECTOR",
    "executive": "EXECUTIVE",
    "internship": "INTERNSHIP",
    "intern": "INTERNSHIP",
}


@dataclass(frozen=True, slots=True)
class Classification:
    category_slug: str
    category_name: str
    seniority_level: str


class JobClassifier:
    """Assigns a role category and seniority level. Pure and deterministic."""

    def classify(
        self,
        title: str,
        *,
        department: str | None = None,
        source_seniority: str | None = None,
    ) -> Classification:
        category = self.classify_category(title, department=department)
        return Classification(
            category_slug=category,
            category_name=CATEGORY_BY_SLUG[category].name,
            seniority_level=self.classify_level(title, source_seniority=source_seniority),
        )

    def classify_category(self, title: str, *, department: str | None = None) -> str:
        """First matching rule wins; rules are ordered by specificity.

        The department is consulted only when the title yields nothing, because it is
        absent for 62% of rows and its values are free text.
        """
        text = (title or "").strip()
        if not text:
            return "other"

        for slug, pattern in _COMPILED:
            if pattern.search(text):
                return slug

        if department:
            for slug, pattern in _COMPILED:
                if pattern.search(department):
                    return slug

        return "other"

    def classify_level(self, title: str, *, source_seniority: str | None = None) -> str:
        """Prefer the source's own value; fall back to the title.

        Only 5% of rows carry one, and values like "Not Applicable" must fall through
        rather than being mapped to a real level.
        """
        if source_seniority:
            mapped = _SOURCE_SENIORITY.get(source_seniority.strip().lower())
            if mapped:
                return mapped

        text = (title or "").strip()
        if not text:
            return "UNKNOWN"

        for level, pattern in _LEVEL_COMPILED:
            if pattern.search(text):
                return level

        return "UNKNOWN"
