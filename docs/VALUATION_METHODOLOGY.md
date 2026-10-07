# Valuation data methodology

How the published market data is produced and how comparable companies are chosen from it. This page describes methods, not decisions about individual companies.

## 1. Data source

* Market figures (price, official trailing P/E, industry classification, financial summaries) come from the Taiwan Stock Exchange (TWSE) and the Taipei Exchange (TPEx) public data. Nothing is scraped from third-party quote sites.
* Each company keeps the date its figures refer to. A weekly release is published only when both exchanges are on a consistent trade date (an explicit, recorded exception exists for seeding); otherwise the previous release stays in place and the gap is reported, not hidden.
* A release carries a manifest with a content hash, per-market dates and a publication gate result, so a consumer can tell exactly which data a number came from.

## 2. Statistical methodology

* Percentiles use the linear-interpolation definition (the "type 7" rule): P25, median, P75. The **median** is the base case; the arithmetic mean is secondary information because it is pulled by extreme multiples.
* A company enters P/E statistics only if its official P/E is positive. Loss-making companies are never included as a multiple.
* Small samples are labelled as small and never padded: fewer than 3 usable companies is "insufficient", 3–4 is "few".

## 3. Outlier policy: ROBUST_IQR_3_V1

* Scope: the positive-P/E companies of **one official industry**.
* Rule: Tukey fence with k = 3 — a P/E is treated as extreme when it lies outside Q1 − 3×IQR … Q3 + 3×IQR.
* The fence is applied only when the sample has at least 8 companies; below that no company is called an outlier.
* There is no fixed P/E ceiling. A wide industry keeps its high-multiple companies as long as they are inside the fence.
* Extreme companies are not deleted: they are excluded from the default comparison, listed separately, and a user may include them by hand (the inclusion is recorded).
* The policy has an id and a version. Results carry both, so a later policy change cannot silently change an earlier result. Older policies (for example a fixed cap of P/E < 200 followed by a k = 1.5 fence) are kept only for audit and comparison; `docs/OUTLIER_POLICY_COMPARISON.md` shows what they would have removed on the same data.

## 4. Taxonomy concept

* **L1** — the exchange's official industry. Used as published; never edited.
* **L2** — an *internal* comparable sub-industry ("who should this company be compared with"). It is always labelled as an internal classification, never as an official one.
* **L3** — structured business-model tags (operating model, process node, technology basis, product) used to explain and order peers. Descriptive tags are allowed, but only structured dimensions count as comparison evidence.
* Every classification carries a version. A saved valuation stores the version it used, so re-classifying later never changes it.
* A classification is shown as either confirmed or waiting for confirmation.

## 5. Comparable methodology

* Candidates come from the same official industry and are grouped in three tiers that are never merged into one ranking: highly comparable, possibly comparable, same-industry reference.
* Business-model similarity is weighed first (40 of 100 points); scale, growth, margins and market cap are secondary. Dimensions with missing data are left out of the score instead of being scored as zero.
* "Highly comparable" requires a confirmed classification, structured business-model evidence on both sides, a similarity above the threshold and no major mismatch (for example a different process node or technology basis). A shared L2 alone is never enough.
* The tool recommends and explains; the final choice of peers is the user's, and every addition or exclusion is recorded with its reason.

## 6. Versioning

* Data releases: content-hashed, dated, immutable once published.
* Outlier policy: id + version.
* Taxonomy: `YYYY.M.N`, with a `.rN` suffix when confirmations are applied on top of a base version.
* Saved valuations freeze the data release, the outlier policy and the taxonomy version they used.

*This is a methodology description. Nothing here is investment advice, a target price or a recommendation.*
