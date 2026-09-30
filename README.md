# Feiran · 斐然

> What oft was thought, but ne’er so well express’d.

English | [简体中文](README.zh-CN.md)

An AI-powered workspace that helps you turn real experience into clearer, job-specific resumes. Understand what a role asks for, refine how you present your experience, and review every change before exporting.

![Feiran — real experience, clearer stories, brighter next steps](web/feiran-brand.png)

## Real experience. Clearer stories. Brighter next steps.

- **Understand the role** — identify requirements in the job description and review anything missed or misclassified.
- **Present relevant experience** — use your confirmed facts to prepare a CV for each job.
- **Refine the wording and structure** — review AI suggestions for phrasing, ordering and what to leave out.
- **Stay in control** — inspect changes, undo them individually, and approve the exact version you want.
- **Export your CV** — download the PDF after reviewing and approving it.

*Same you. A stronger story.*

## Availability

A single-owner Feiran workspace is now deployed on AWS with a public HTTPS address and private login. **There is no public demo or open registration.** The owner can open the workspace in a browser; local operation remains optional. Login protection, synthetic English/Chinese PDF export, persistence across test containers, and backup restoration have been verified on AWS. The cloud DeepSeek secret is connected, and a bounded live API check passed. Full resume quality still requires your review. See [AWS public deployment](docs/aws-public.md) and the [acceptance record](docs/aws-acceptance.md) for the tested scope and remaining checks.

## Using Feiran

Once you have access to a running workspace:

1. **Bring your experience.** Choose Chinese or English when uploading a PDF; both CVs are kept separately. Check and confirm the extracted facts. Editing a fact saves a new version that needs confirmation.
2. **Choose a job.** In My jobs → New job, enter any public posting URL and choose Read and generate. Greenhouse, Lever, Ashby and Tencent links use their public APIs; other sites are read as webpages, preferring structured JobPosting data. Feiran reads the title and description, then prepares a CV draft for review. Check the extracted text for completeness. Login, CAPTCHA, JavaScript-only pages and incomplete responses may require manual pasting. Repeating a saved URL opens the existing job. You can also browse followed companies.
3. **Review your tailored CV.** Check the job requirements, proposed wording and layout changes. See which requirements your CV supports, which evidence was left out, and where information is missing. Add suggested experience only when it is true for you.
4. **Approve and export.** Read the CV and every change, approve that version, and download the final PDF. Changed content needs a new review.

Switch between **English** and **简体中文** at the top of the page. The interface remembers your choice; changing the interface language does not translate your job descriptions, experience statements or CV. CV languages depend on the information in your profile.

Choose an uploaded Chinese or English CV for each job. Uploading again replaces only that language and keeps the previous profile in history. After related facts or profile content change, the old draft remains viewable but must be regenerated and reviewed before final export. PDFs need extractable text; scanned documents are not supported.

All regions share one listings database; Mainland China, other regions and unknown-location filters use explicit workplace locations. You can also add Tencent's public board at `https://careers.tencent.com/zh-cn/search.html`. Its list contains responsibility summaries; full requirements are fetched on selection. BOSS, ByteDance and Alibaba automatic refresh adapters are not yet included; paste their full descriptions instead. Cached postings reflect the time they were fetched; check the official page before applying.

## Your information and your decisions

- Your experience and saved materials stay in the private data directory of your Feiran instance. In a cloud workspace, this is on the cloud host; local operation stores it on your computer.
- AI features send relevant job and experience text to DeepSeek. The application filters known identifying details and uses temporary references for experience statements. Filtering does not guarantee anonymity; experience text can still be identifying.
- Suggestions are not new facts. You decide whether an addition is true and which wording to approve.
- Approval belongs to the version you reviewed. Changing the content or relevant facts can require you to review and approve again.

## Current limits

AI can misunderstand a requirement or change the meaning of a sentence. Automated checks catch some unsupported changes, but they cannot verify your real experience or replace your review.

Job ordering is based on skill-word overlap, not eligibility or the probability of an offer. Missing evidence does not mean you lack a qualification, and a saved job description does not prove a role is still open.

Feiran prepares and reviews materials. It does not submit applications, track applications or predict hiring outcomes. Optional semantic review remains an unvalidated experiment and is not part of the normal web workflow.
