"""A synthetic CV shaped like the reported two-page case: two schools, two internships, two
projects, four skills lines and two papers (20 lines), worded at the length a model rewrite
gives, for a backend job. No real person, school or employer."""

PROFILE = {
    "profile_version": 1,
    "name": "Jordan Sample",
    "contact": {"location": "Example City, CA", "phone": "000-000-0000", "email": "jordan@example.com",
                "links": [{"label": "github.com/jordan-sample", "url": "https://github.com/jordan-sample"}]},
    "sections": [
        {"kind": "education", "entries": [
            {"title": "Example State University", "location": "Example City, CA",
             "subtitle": "Master of Science in Computer Science", "dates": "Aug 2025 – May 2027",
             "facts": ["fact-ms-course"]},
            {"title": "Sample Institute of Technology", "location": "Sample Town",
             "subtitle": "Bachelor of Engineering in Software Engineering", "dates": "Sep 2021 – Jun 2025",
             "facts": ["fact-be-course"]},
        ]},
        {"kind": "experience", "entries": [
            {"title": "Example Logistics Co.", "location": "Example City", "subtitle": "Backend Engineering Intern",
             "dates": "Jun 2025 – Aug 2025", "facts": ["fact-x1-api", "fact-x1-cache", "fact-x1-tests", "fact-x1-docs"]},
            {"title": "Sample Analytics Ltd.", "location": "Sample Town", "subtitle": "Software Engineering Intern",
             "dates": "Jun 2024 – Sep 2024", "facts": ["fact-x2-pipeline", "fact-x2-dashboard", "fact-x2-oncall"]},
        ]},
        {"kind": "projects", "entries": [
            {"title": "Course Planner", "location": "Python, FastAPI, PostgreSQL",
             "facts": ["fact-p1-api", "fact-p1-search", "fact-p1-ci"]},
            {"title": "Photo Sorter", "location": "Swift, Core ML", "facts": ["fact-p2-app", "fact-p2-model"]},
        ]},
        {"kind": "skills", "entries": [{"facts": ["fact-sk-lang", "fact-sk-backend", "fact-sk-data", "fact-sk-ml"]}]},
        {"kind": "publications", "entries": [{"facts": ["fact-pub-1", "fact-pub-2"]}]},
    ],
}

TEXTS = {
    "fact-ms-course": "Coursework: Distributed Systems, Database Systems, Cloud Computing, Machine Learning, Computer Networks",
    "fact-be-course": "Coursework: Data Structures, Operating Systems, Software Engineering, Computer Graphics, Linear Algebra",
    "fact-x1-api": "Designed and built REST APIs in Java and Spring Boot for an internal scheduling platform used by 40 dispatch "
                "teams, cutting manual booking work by 35% and handling 2M requests per day with p99 latency under 120 ms.",
    "fact-x1-cache": "Introduced a Redis caching layer and query optimizations on PostgreSQL for the scheduling platform, "
                  "reducing database load by 45% and improving median API response time from 210 ms to 80 ms.",
    "fact-x1-tests": "Added unit and integration tests with JUnit and Testcontainers, raising coverage of the scheduling "
                  "services from 52% to 86% and catching three regressions before release through the CI pipeline.",
    "fact-x1-docs": "Wrote onboarding documentation and runbooks for the scheduling platform, and presented the caching "
                 "design to the wider engineering team in a recorded knowledge-sharing session for new hires.",
    "fact-x2-pipeline": "Built a Python and Apache Airflow data pipeline that ingested 30 GB of daily event logs into "
                     "BigQuery, replacing manual exports and giving analysts fresh data every hour instead of every day.",
    "fact-x2-dashboard": "Created internal dashboards with Looker and SQL for the customer success team, tracking churn "
                      "signals across 1,200 accounts and helping the team prioritize outreach to at-risk customers.",
    "fact-x2-oncall": "Joined the team on-call rotation, triaging pipeline alerts and writing postmortems for two "
                   "incidents, which led to new data quality checks that reduced failed nightly jobs by 60%.",
    "fact-p1-api": "Built a course planning web app with FastAPI and PostgreSQL that lets students compare schedules, "
                "serving 800 weekly users with Docker deployment on a small cloud virtual machine.",
    "fact-p1-search": "Implemented full-text search with PostgreSQL indexes and ranking, returning course results in "
                   "under 50 ms across 12,000 sections and supporting filters by time, instructor and requirement.",
    "fact-p1-ci": "Set up GitHub Actions continuous integration with linting, type checks and automated tests, so every "
               "pull request was verified before merge and deployments became repeatable.",
    "fact-p2-app": "Developed an iOS app in Swift that sorts photos into albums on device, with a SwiftUI interface and "
                "background processing that keeps the phone responsive while sorting large libraries.",
    "fact-p2-model": "Trained and converted an image classification model to Core ML, reaching 91% accuracy on a "
                  "held-out set of 5,000 personal photos while keeping inference under 30 ms per image.",
    "fact-sk-lang": "Languages: Java, Python, SQL, Go, TypeScript, Swift, C++, Bash",
    "fact-sk-backend": "Backend: Spring Boot, FastAPI, REST APIs, gRPC, Redis, PostgreSQL, Kafka, Docker, Kubernetes, AWS",
    "fact-sk-data": "Data: Apache Airflow, BigQuery, Pandas, Looker, dbt, Spark",
    "fact-sk-ml": "Machine learning: PyTorch, scikit-learn, Core ML, computer vision, model evaluation",
    "fact-pub-1": "J. Sample, A. Writer and B. Author. Comparing Retrieval Methods for Course Search on Small Campus "
               "Datasets. Proceedings of the Example Workshop on Educational Data Systems, 2025, pp. 12–19.",
    "fact-pub-2": "J. Sample and C. Coauthor. Lightweight On-Device Photo Classification with Distilled Convolutional "
               "Networks. Sample Journal of Mobile Computing, vol. 4, no. 2, 2024, pp. 33–41.",
}

FACTS = {fact_id: {"id": fact_id, "version": 1, "text": text, "status": "confirmed", "type": "other", "tags": []}
         for fact_id, text in TEXTS.items()}

REQUIREMENTS = [
    ("r1", "Build and operate REST APIs and backend services in Java or Python", "required"),
    ("r2", "Experience with PostgreSQL, Redis and caching for performance", "required"),
    ("r3", "Write automated tests and work with CI pipelines", "preferred"),
    ("r4", "Familiarity with Docker, Kubernetes and AWS", "preferred"),
]
JD_TEXT = "Backend Software Engineer\n" + "\n".join(text for _id, text, _strength in REQUIREMENTS)
JOB = {
    "jd": {"title": "Backend Software Engineer", "company": "Example Corp", "source": "https://example.com/jobs/1",
           "captured_at": "2026-10-01T00:00:00+00:00", "text": JD_TEXT},
    "facts": [],
    "selected_requirements": [{"id": rid, "text": text, "strength": strength} for rid, text, strength in REQUIREMENTS],
}

ADDED_BULLET = ("Deployed the scheduling services to Kubernetes on AWS with Helm charts and autoscaling, keeping "
                "availability above 99.9% during the peak season and halving the time to ship a release.")
