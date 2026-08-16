# PPS API Catalog v0.1.0

This catalog was extracted from the eighteen PPS reference documents in the
authorized workspace. The documents describe v1.x services, while some current
data.go.kr deployments use `*Service02`; live base URLs, operation names and
response fields must be verified before production activation.

## P0 services

### Bid notice information

Service candidates: `BidPublicInfoService`, `BidPublicInfoService02`

Priority operation candidates:

- `getBidPblancListInfoServc`
- `getBidPblancListInfoServcPPSSrch`
- `getBidPblancListInfoEorderAtchFileInfo`
- `getBidPblancListInfoLicenseLimit`
- `getBidPblancListInfoPrtcptPsblRgn`
- `getBidPblancListInfoServcBsisAmount`
- `getBidPblancListPPIFnlRfpIssAtchFileInfo`

Expected use: notice identity, title, agency, dates, budget, contract method,
eligibility constraints, regions and attachment URLs.

### Opening and award information

Service: `ScsbidInfoService`

- `getOpengResultListInfoServc`
- `getOpengResultListInfoServcPPSSrch`
- `getScsbidListSttusServc`
- `getScsbidListSttusServcPPSSrch`

Expected use: participants, bid prices, ranks, successful bidder and failed or
rebid states.

### Contract information

Service: `CntrctInfoService`

- `getCntrctInfoListServc`
- `getCntrctInfoListServcPPSSrch`
- `getCntrctInfoListServcChgHstry`
- `getCntrctInfoListServcDltHstry`

Expected use: contracting party, original/changed contract amount, contract
period and change history.

### Prior specification

Service: `HrcspSsstndrdInfoService`

- `getPublicPrcureThngInfoServc`
- `getPublicPrcureThngInfoServcPPSSrch`
- `getPublicPrcureThngOpinionInfoServc`
- `getThngDetailMetaInfoServc`

Expected use: early opportunity discovery, assigned budget, specification
metadata and opinions.

## P1 services

### Order plan

Service: `OrderPlanSttusService`

- `getOrderPlanSttusListServc`
- `getOrderPlanSttusListServcPPSSrch`
- `getOrderPlanSttusAtchFileList`

### Supplier and qualification reference

- `UsrInfoService02`: organization and registered-industry facts;
- `IndstrytyBaseLawrgltInfoService`: industry codes and legal basis.

These reference services may enrich candidate matching but cannot replace
deadline-valid company evidence.

## Collection contract

- store request parameters, retrieval time and response hash;
- normalize encoding and never log `serviceKey`;
- paginate until an explicit terminal condition;
- split date windows when response density exceeds the API limit;
- retry 429/5xx with bounded exponential backoff and jitter;
- treat an empty successful response differently from an API error;
- deduplicate with notice number + round, then create a new version when source
  metadata or attachment hashes change;
- retain raw responses in private object storage for reproducibility;
- route schema drift to a dead-letter queue instead of silently dropping fields.

## Verification gate

An API moves from `PENDING` to `VERIFIED` only when authentication, one real
download, pagination, required-field mapping, error handling and a repeatable
fixture have all passed. Possessing an API key alone does not satisfy this gate.

### Live connectivity check - 2026-08-16

The configured PPS key was exercised without logging the credential. All five
service families returned header result code `00` with non-empty results:

| Service | Tested operation | Result count | Current gate |
|---|---|---:|---|
| Bid notice | `getBidPblancListInfoServcPPSSrch` | 567 | authentication/download verified |
| Award | `getScsbidListSttusServcPPSSrch` | 300 | authentication/download verified |
| Contract | `getCntrctInfoListServc` | 15,466 | authentication/download verified |
| Prior specification | `getPublicPrcureThngInfoServc` | 259 | authentication/download verified |
| Order plan | `getOrderPlanSttusListServcPPSSrch` | 7 | authentication/download verified |

Counts describe the fixed historical query windows used by the connectivity
check and are not business metrics. Pagination, full field mapping and
incremental-watermark verification remain open before the final `VERIFIED`
gate.
