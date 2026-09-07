# Submission confirmation items

The detail view's `확인 필요 사항` list has ten fixed items: 전자입찰 여부,
입찰 마감일, 제안서 마감일, 제안서 제출 유형, 입찰보증보험,
총괄책임자 PT 진행 여부, 연수원·강의장 보유 여부, 참여인력 자격조건,
정산 여부, and 비영리 이윤제외.

This frontend projection groups existing accepted extraction quotes by topic.
It displays the complete quote, including negation and exceptions, with document
and page/section references. A link is shown only when exactly one existing
evidence card matches the document, location and quote; identical quotes in
another document or location cannot supply the link. Topic
matching does not determine applicability, company compliance, or a yes/no
answer. Missing support displays `원문 확인 필요`. Normalized summaries and
company values cannot fill a missing quote. When attachment status is supplied,
only documents listed as analyzed contribute quotes. Duplicate filenames are
ambiguous and do not contribute quotes without a unique current source.

The public notice bid deadline is additionally displayed with its metadata
source and existing public notice link. It is never used to fill the proposal
deadline. Proposal submission methods require an explicit method in the quote;
staff qualifications and lead presentation duties are separate topics. Multiple
source statements, including conflicting ones, remain visible for review.

The complete eligibility panel, four-class policy review, evidence panel,
deterministic evaluator and scoring engine remain independent. Cancelled notices
retain their existing hidden-action behavior. This change adds no extraction
prompt, model call, API field, persistence, or production operation.
