# 08 — Two boards: US and India

The platform kept one country. It now keeps two, switched in the header, and almost none of
the work was in the switch.

---

## 1. Why a cookie and not a URL segment

`/us/jobs` and `/in/jobs` would be self-describing, and were rejected anyway: every existing
URL would move, all seven routes would take a segment they mostly ignore, and the segment
would have to be threaded through every internal link on the site. A cookie scopes the whole
board from one place.

The cost is real and worth stating: **a link is no longer self-describing.** `/jobs` means
"jobs on whichever board you last chose", so a URL shared between two people can show them
different things. `?country=` is honoured where it appears and wins over the cookie, which is
the escape hatch for anything that must be shareable.

The switcher writes the cookie from the client and calls `router.refresh()`. Every page is
already `force-dynamic`, so the refresh re-runs them against the new cookie and no route
needs to know the switcher exists.

---

## 2. The normalizer was built to confirm US or reject

This was the actual work. `LocationNormalizer` had one question to answer — *is this
American?* — and three ways to say no:

| signal | old behaviour |
|---|---|
| state is a foreign subdivision (`Maharashtra`) | reject, `country_code = None` |
| city is a known foreign city (`Bengaluru`, `Pune`, `Noida`) | reject |
| foreign country + any state code | keep the country, **drop the subdivision** |

Every Indian row hits at least one of those, so turning on `INGEST_COUNTRY_ALLOWLIST=US,IN`
alone would have changed nothing: the rows arrive with `country_code = None` and are rejected
one step later as an invalid country.

What changed, and what deliberately did not:

- **Country is resolved before the state.** A foreign subdivision is only disqualifying when
  it belongs to a country we do not keep. `India` + `Maharashtra` now resolves; `Canada` +
  `Ontario` still rejects.
- **`United States` + `Maharashtra` still rejects.** That contradiction check is the
  strongest disqualifying signal in the file — the source really does pair a US country with
  a foreign state — and India support must not weaken it. So an Indian subdivision only
  counts when the country field agrees or is absent.
- **`NON_US_CITIES` no longer applies to a row already resolved as Indian.** That list exists
  to defend the US feed, and half of it is exactly the set of cities an Indian job names.
- **A city still cannot promote itself to a country.** `Bengaluru` with no country is still
  rejected, in both directions. The rule was never "prefer the US"; it was "one weak signal
  is not evidence".

---

## 3. `IN` is India and Indiana

India's ISO country code is `IN`. Indiana's state code is also `IN`.

They never collide in storage — `jobs.country_code` and `jobs.state_code` are different
columns — but they would collide instantly in a shared lookup table, where `"IN"` would mean
whichever entry was written last. So `resolve_state_code` (US) and `resolve_india_state_code`
stay separate functions over separate tables, and an Indian row ignores anything the US
resolver produced:

```
India + state "IN" + city "Chennai"  ->  country=IN  state=TN   (from the city, not the code)
United States + "Indiana"            ->  country=US  state=IN
```

Both are pinned by tests.

---

## 4. Indian rows arrive shaped differently

A US row is confirmed by its state code. An Indian row usually carries a city and an empty
state field, so `INDIA_CITY_TO_STATE` fills in the subdivision for cities whose state is
unambiguous — Bengaluru→KA, Pune→MH, Noida→UP, Gurugram→HR. A city that exists in two states
is not in that map and stays without a subdivision, which the "Jobs by state" panel simply
omits rather than bucketing as "unknown".

Volume, measured at the source (`docs/00-source-verification.md` §6): **909 India rows per
day against 62,425 US.** After the role allowlist the India board will be a fraction of that.
It also starts **empty** — rejected rows were never stored, so there is no history to reveal,
only new days to accumulate.

---

## 5. What the API scopes, and what it does not

`stats`, `by_state` and all three facet endpoints take a country. `stats` previously carried a
`us_jobs` column; under a country scope that either duplicates `total_jobs` or reads zero, so
it is gone rather than kept for compatibility.

Not scoped, deliberately:

- **Job detail** (`/jobs/{id}`) — an id identifies one job, whatever board found it.
- **The employer directory** (`/companies`) — a company is not per-country, and splitting it
  would mean deciding what a company with offices in both is. It shows every employer.

---

## 6. Interaction with the fetch cache

Adding `?country=` to the facet endpoints nearly disabled their caching, because the rule
introduced after the inode incident only cached a *parameterless* call on a bounded path.
`BOUNDED_PARAMS` now names the params whose value set is small enough not to matter, and
`country` has two values: a bounded path with a country is two cache entries, not the
unbounded set that filled a host's disk. Nothing else belongs in that list — `state` alone
would be fifty times every path it appears on.
