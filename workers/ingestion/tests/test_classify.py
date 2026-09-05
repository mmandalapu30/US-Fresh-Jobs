"""JobClassifier tests.

Most cases here come from titles that were *actually* misclassified while tuning against
95,030 real jobs. They exist to stop those specific regressions returning.
"""

from __future__ import annotations

import re

import pytest

from ingestion.services.classify import (
    _RULES,
    CATEGORIES,
    CATEGORY_BY_SLUG,
    SENIORITY_LEVELS,
    JobClassifier,
)


@pytest.fixture
def classifier() -> JobClassifier:
    return JobClassifier()


class TestTaxonomy:
    def test_every_rule_targets_a_real_category(self) -> None:
        for slug, _ in _RULES:
            assert slug in CATEGORY_BY_SLUG, f"rule targets unknown category {slug!r}"

    def test_every_pattern_compiles(self) -> None:
        for slug, pattern in _RULES:
            try:
                re.compile(pattern)
            except re.error as exc:  # pragma: no cover - a failure is the point
                pytest.fail(f"{slug} does not compile: {exc}")

    def test_slugs_are_unique(self) -> None:
        slugs = [c.slug for c in CATEGORIES]
        assert len(slugs) == len(set(slugs))

    def test_other_is_the_fallback_and_has_no_rule(self) -> None:
        """ "other" must be reachable only by falling through every rule."""
        assert "other" in CATEGORY_BY_SLUG
        assert "other" not in {slug for slug, _ in _RULES}


class TestCategoryAssignment:
    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            # healthcare -- the largest real category
            ("Registered Nurse (RN)", "healthcare"),
            ("Licensed Practical Nurse", "healthcare"),
            ("Medical Assistant", "healthcare"),
            ("Dental Hygienist", "healthcare"),
            ("Ultrasound Technologist", "healthcare"),
            ("Direct Support Professional (DSP)", "healthcare"),
            # education
            ("Elementary Teacher", "education"),
            ("Substitute Teacher", "education"),
            # software vs engineering -- a boundary worth pinning down
            ("Software Engineer", "software"),
            ("Senior Full Stack Developer", "software"),
            ("Mechanical Engineer", "engineering"),
            ("Civil Engineer", "engineering"),
            # trades / manufacturing / construction
            ("Maintenance Technician", "skilled-trades"),
            ("HVAC Technician", "skilled-trades"),
            ("Machine Operator", "manufacturing"),
            ("Construction Cost Estimator", "construction"),
            # transport
            ("CDL Bus Driver", "transport"),
            ("Warehouse Associate", "transport"),
            # service
            ("Line Cook", "food-hospitality"),
            ("Barista", "food-hospitality"),
            ("Cashier", "retail-customer"),
            ("Customer Service Representative", "retail-customer"),
            # business functions
            # "Sales Associate" is a retail floor role in this dataset, sitting
            # alongside Cashier and Team Member -- not a B2B seller.
            ("Sales Associate", "retail-customer"),
            ("Account Executive", "sales"),
            ("Financial Advisor", "sales"),
            ("Staff Accountant", "finance"),
            ("Paralegal", "legal"),
            ("Recruiter", "hr"),
            ("Social Worker", "social-services"),
            ("Administrative Assistant", "admin"),
            ("General Manager", "management"),
        ],
    )
    def test_known_titles(self, classifier: JobClassifier, title: str, expected: str) -> None:
        assert classifier.classify_category(title) == expected

    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            # REGRESSION: a trailing \b in the rule pattern made every truncated stem
            # unmatchable, silently dumping these into "other".
            ("Manufacturing Associate", "manufacturing"),
            ("Biologist", "science"),
            ("Recruiting Coordinator", "hr"),
            ("Housekeeping Attendant", "construction"),
            ("Data Scientist", "data-science"),
            ("Landscaping Crew", "construction"),
            ("Underwriting Analyst", "finance"),
        ],
    )
    def test_stem_inflections_match(
        self, classifier: JobClassifier, title: str, expected: str
    ) -> None:
        assert classifier.classify_category(title) == expected

    @pytest.mark.parametrize(
        ("title", "not_expected"),
        [
            # REGRESSION: bare "principal" captured engineers as school staff.
            ("Principal Software Engineer", "education"),
            ("Principal Electronics Engineer", "education"),
            # REGRESSION: bare "tech" captured any specialist as a tradesperson.
            ("Senior Tech Specialist, Applications", "skilled-trades"),
            # bare "officer" would have made every C-suite title a security job.
            ("Chief Executive Officer", "security"),
            ("Chief Financial Officer", "security"),
            # "Pilot Program Manager" is not aviation.
            ("Pilot Program Manager", "transport"),
            # REGRESSION: ABA roles read as "technician".
            ("Registered Behavior Technician (RBT)", "skilled-trades"),
        ],
    )
    def test_greedy_matches_are_prevented(
        self, classifier: JobClassifier, title: str, not_expected: str
    ) -> None:
        assert classifier.classify_category(title) != not_expected

    def test_behavior_technician_is_healthcare(self, classifier: JobClassifier) -> None:
        assert classifier.classify_category("Registered Behavior Technician (RBT)") == "healthcare"

    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            # The three-way split: data / programming / infrastructure are different
            # products to a job seeker, so each must resolve distinctly.
            # The three data disciplines are distinct products: an engineer builds the
            # pipelines, an analyst queries them, a scientist models on top.
            ("Data Engineer III", "data-engineering"),
            ("Analytics Engineer", "data-engineering"),
            ("ETL Developer", "data-engineering"),
            ("Data Warehouse Architect", "data-engineering"),
            ("Data Analyst", "data-analytics"),
            ("Business Intelligence Analyst", "data-analytics"),
            ("Reporting Analyst", "data-analytics"),
            ("Senior Data Scientist", "data-science"),
            ("Machine Learning Engineer", "data-science"),
            ("AI Engineer", "data-science"),
            ("Backend Engineer", "software"),
            ("DevOps Engineer", "software"),
            ("QA Engineer", "software"),
            ("IT Support Specialist", "it-ops"),
            ("Help Desk Technician", "it-ops"),
            ("Database Administrator", "it-ops"),
            ("Salesforce Administrator", "it-ops"),
        ],
    )
    def test_tech_split(self, classifier: JobClassifier, title: str, expected: str) -> None:
        assert classifier.classify_category(title) == expected

    @pytest.mark.parametrize(
        "title",
        [
            # The first four were read out of the stored rows before the category
            # existed, each filed under a different slug by whichever ordinary word its
            # title happened to contain.
            "Workday Developer Sr",
            "Business Analyst II - Workday",
            "Sr Mgr, Workday Platform Engineering",
            "Project Manager, Salesforce or Workday experience",
            "Workday Integration Consultant",
            "Workday HCM Functional Analyst",
            "Workday Studio Developer",
            "Workday Report Writer",
            "HRIS Manager (Workday)",
            # Module names that read as ordinary scheduling words. These are the reason
            # the ordinary-sense guard below is a lookbehind and not a lookahead.
            "Workday Time Tracking & Scheduling Consultant",
            "Workday Payroll Specialist",
        ],
    )
    def test_workday_titles_are_their_own_category(
        self, classifier: JobClassifier, title: str
    ) -> None:
        """A Workday role is one market, whatever noun the title reaches for.

        `software`, `data-analytics` and `it-ops` each took a share of these, which left
        the attribute they actually share unfilterable.
        """
        assert classifier.classify_category(title) == "workday"

    @pytest.mark.parametrize(
        "title",
        [
            "Warehouse Associate - Flexible Workday",
            "Machine Operator (Compressed Workday)",
            "Delivery Driver - 4-day workday",
            "Retail Associate, Standard Workday",
        ],
    )
    def test_the_ordinary_sense_of_workday_is_not_a_workday_role(
        self, classifier: JobClassifier, title: str
    ) -> None:
        """The product name is also a plain English word.

        Same class of bug as bare `spark` matching "SPARK AmeriCorps Member": a tool
        name that doubles as a common word needs qualifying, or a warehouse shift
        pattern lands on an HRIS board.
        """
        assert classifier.classify_category(title) != "workday"

    def test_workday_sits_behind_healthcare_and_education(self, classifier: JobClassifier) -> None:
        """The rule is inserted ahead of the tech block, not ahead of the taxonomy.

        Those two domains outrank tooling by an existing, deliberate property of the
        ordering — the same reason "Clinical Data Manager" stays healthcare.
        """
        assert classifier.classify_category("Clinical Workday Analyst - Patient Care") == (
            "healthcare"
        )

    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            # The data rules must not steal roles that merely contain a stray keyword.
            ("Financial Analyst", "finance"),
            ("Registered Nurse", "healthcare"),
            ("Mechanical Engineer", "engineering"),
            ("Elementary Teacher", "education"),
            ("Data Entry Clerk", "admin"),
        ],
    )
    def test_data_rules_do_not_over_reach(
        self, classifier: JobClassifier, title: str, expected: str
    ) -> None:
        assert classifier.classify_category(title) == expected

    @pytest.mark.parametrize(
        "title",
        [
            # REGRESSION: bare "systems engineer" in the software rule pulled mechanical
            # and aerospace roles onto a data/software board. Found by reading real rows:
            # "Thermal and Fluid Systems Engineer" was filed as software.
            "Thermal and Fluid Systems Engineer",
            "Systems Engineer",
            "Senior Systems Engineer",
            "Principal Systems Engineer",
            "Applications Engineer",
        ],
    )
    def test_ambiguous_engineer_titles_are_not_claimed_by_software(
        self, classifier: JobClassifier, title: str
    ) -> None:
        """An ambiguous engineering title belongs in `engineering`, not `software`.

        Precision matters more than recall here: a mechanical engineer showing up on a
        software board is worse than a genuine software systems engineer being missed.
        """
        assert classifier.classify_category(title) != "software"

    @pytest.mark.parametrize(
        "title",
        [
            "Platform Engineer",
            "SaaS Platform Engineer",
            "Solutions Architect",
            "Integration Engineer",
            "Automation Engineer - Controls & Python",
        ],
    )
    def test_genuine_software_titles_survive_the_narrowing(
        self, classifier: JobClassifier, title: str
    ) -> None:
        assert classifier.classify_category(title) == "software"

    @pytest.mark.parametrize(
        "title",
        [
            "Front End - Cashier",
            "Front End Clerk",
            "Front End Supervisor - Grocery",
            "Laser and CNC Punch Machine Programmer",
            "CNC Programmer",
            "PLC Programmer",
        ],
    )
    def test_retail_and_machine_titles_are_not_claimed_by_software(
        self, classifier: JobClassifier, title: str
    ) -> None:
        """Bare `front end` and bare `programmer` used to pull these onto the board.

        Same reasoning as the systems-engineer case above: a cashier or a toolpath
        programmer on a software board is worse than missing an ambiguous title.
        """
        assert classifier.classify_category(title) != "software"

    @pytest.mark.parametrize(
        "title",
        [
            "Front End Developer",
            "Front-End Engineer",
            "Frontend Engineer",
            "Backend Developer",
            "Senior Front End Web Developer",
            "COBOL Programmer",
            "Programmer Analyst",
        ],
    )
    def test_genuine_front_end_and_programmer_titles_survive(
        self, classifier: JobClassifier, title: str
    ) -> None:
        assert classifier.classify_category(title) == "software"

    def test_clinical_titles_beat_management(self, classifier: JobClassifier) -> None:
        """Healthcare is checked first precisely so this does not become "management"."""
        assert classifier.classify_category("Clinical Director") == "healthcare"
        assert classifier.classify_category("Nurse Manager") == "healthcare"

    def test_department_is_a_fallback_only(self, classifier: JobClassifier) -> None:
        """Department is absent for 62% of rows, so it must never override a clear title."""
        assert classifier.classify_category("Registered Nurse", department="Sales") == "healthcare"
        assert classifier.classify_category("Specialist II", department="Nursing") == "healthcare"

    @pytest.mark.parametrize("title", ["", "   ", "Zzzqqq Wibble"])
    def test_unclassifiable_falls_through_to_other(
        self, classifier: JobClassifier, title: str
    ) -> None:
        assert classifier.classify_category(title) == "other"

    def test_result_is_deterministic(self, classifier: JobClassifier) -> None:
        title = "Senior Registered Nurse - ICU"
        assert classifier.classify(title) == classifier.classify(title)


class TestSeniorityLevel:
    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            ("Senior Software Engineer", "SENIOR"),
            ("Sr. Accountant", "SENIOR"),
            ("Junior Developer", "ENTRY"),
            ("Engineering Intern", "INTERNSHIP"),
            ("Team Lead", "LEAD"),
            ("Store Manager", "MANAGER"),
            ("Shift Supervisor", "MANAGER"),
            ("Director of Nursing", "DIRECTOR"),
            ("Chief Financial Officer", "EXECUTIVE"),
            ("Vice President, Sales", "EXECUTIVE"),
        ],
    )
    def test_levels_from_title(self, classifier: JobClassifier, title: str, expected: str) -> None:
        assert classifier.classify_level(title) == expected

    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            # The strongest signal must win over a weaker one in the same title.
            ("Executive Director", "EXECUTIVE"),
            ("Senior Director, Finance", "DIRECTOR"),
            ("Senior Manager, Operations", "MANAGER"),
            ("Lead Software Engineer", "LEAD"),
        ],
    )
    def test_strongest_signal_wins(
        self, classifier: JobClassifier, title: str, expected: str
    ) -> None:
        assert classifier.classify_level(title) == expected

    def test_source_seniority_is_preferred(self, classifier: JobClassifier) -> None:
        """Only 5% of rows carry one, but where present it beats a title guess."""
        assert classifier.classify_level("Analyst", source_seniority="Director") == "DIRECTOR"

    def test_unmappable_source_value_falls_through(self, classifier: JobClassifier) -> None:
        """ "Not Applicable" must not become a real level."""
        assert (
            classifier.classify_level("Senior Analyst", source_seniority="Not Applicable")
            == "SENIOR"
        )

    def test_no_signal_is_unknown_not_guessed(self, classifier: JobClassifier) -> None:
        """Most titles state no level. Inventing "MID" would be a fabrication."""
        assert classifier.classify_level("Cashier") == "UNKNOWN"

    def test_every_produced_level_is_declared(self, classifier: JobClassifier) -> None:
        titles = [
            "Intern",
            "Junior Analyst",
            "Senior Engineer",
            "Team Lead",
            "Store Manager",
            "Director of Ops",
            "Chief Executive Officer",
            "Cashier",
        ]
        for title in titles:
            assert classifier.classify_level(title) in SENIORITY_LEVELS


class TestOrthogonality:
    """Category and level must be independent, or neither filter is useful alone."""

    @pytest.mark.parametrize(
        ("title", "category", "level"),
        [
            ("Sales Manager", "sales", "MANAGER"),
            ("Senior Registered Nurse", "healthcare", "SENIOR"),
            ("Director of Software Engineering", "software", "DIRECTOR"),
            ("Warehouse Associate", "transport", "ENTRY"),
            ("Chief Nursing Officer", "healthcare", "EXECUTIVE"),
        ],
    )
    def test_both_dimensions_resolve(
        self, classifier: JobClassifier, title: str, category: str, level: str
    ) -> None:
        result = classifier.classify(title)
        assert result.category_slug == category
        assert result.seniority_level == level

    def test_classification_carries_the_display_name(self, classifier: JobClassifier) -> None:
        result = classifier.classify("Registered Nurse")
        assert result.category_name == "Healthcare & Nursing"
