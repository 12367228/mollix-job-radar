# Sanitised fixtures

These small HTML documents were written by hand. They reproduce only the public
listing structure observed on 2026-09-21, not whole site HTML or real orders.
Titles, identifiers, amounts, descriptions and dates are invented. There are no
usernames, contacts, cookies, embedded application state or credentials.

`freelance_ru.html`: public/premium cards, budget range, deadline, counts, invalid
link and missing optional fields. `weblancer.html`: heading budget, text summary,
date-only publication, tags, grouped numbers and invalid cards. Other fixtures
exercise layout drift and an access challenge. Tests never fetch live pages.

`relevance_cases.json` contains the ten requested synthetic relevance/risk/budget
examples. No live listings, private data, or credentials are included.

Discovery fixtures added on 2026-09-23:

- `fl_ru.html`: hand-written public listing structure, relative timestamp, price,
  response count, vacancy exclusion and an invalid link.
- `telegram_public.html`: hand-written web-preview structure, timezone timestamps,
  a technical order, ad, course, full-time vacancy, nontechnical post, missing date,
  cross-source repost, unexpected channel and a photo-only post.
- `habr_freelance.html`: **synthetic parser contract, NOT a verified live layout**.
  Habr's robots.txt disallows `/`; no protected listing was fetched. This fixture
  tests fail-closed parsing/normalization, not production availability of Habr.
