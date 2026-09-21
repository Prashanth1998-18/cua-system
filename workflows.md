# Workflows

Plain-English procedures for each SauceDemo workflow, written the way you'd brief a new
staff member. These are given to the discovery agent as its goal, so the agent only has
to find things on the screen, not guess what the business process is. See `decisions.md`
ADR-011 for why, and ADR-013 to ADR-017 for the decisions behind these three.

Every workflow logs in with the configured account (username and password, given to you).
Discovery always uses `standard_user`.

---

## Browse catalog

Answers "what do you sell?" and "how much does X cost?".

**You're given:** a username, a password, and a sort order: one of "Name (A to Z)",
"Name (Z to A)", "Price (low to high)", "Price (high to low)".

**What to do:**
1. Go to https://www.saucedemo.com and log in with the username and password.
2. Wait for the Products page to finish loading.
3. Choose the given sort order in the sort dropdown.
4. Read every product on the page: its name, description and price, in the order shown.

**Returns:** the list of products, each with its name, description and price.

**Success:** the Products page is showing, sorted as requested, and every product on it has
been read.

**Failure (a legitimate "no"):** the site rejects the login (for example the account is locked
out). Report "login rejected" with the reason the site shows. Otherwise none: the catalog
always has an answer.

**Error (escalate to a human):** a page never finishes loading even after one retry, or
anything else doesn't look like what's described above. Stop, report which step, what we expected, and what we actually saw, with
a screenshot, and hand it to a human.

---

## Total with tax

Only for when someone explicitly asks for the cost *including tax*. For the price of an item,
use Browse catalog instead.

**You're given:** a username, a password, a list of product names (exactly as they appear in
the catalog, each at most once), and the customer's first name, last name and zip code. The
zip code must be an existing 5-digit US zip code.

**What to do:**
1. Go to https://www.saucedemo.com and log in with the username and password.
2. Wait for the Products page to finish loading.
3. For each product in the list, click the "Add to cart" button on that product.
4. Open the cart, and click Checkout.
5. Enter the customer's first name, last name and zip code, and click Continue.
6. On the review page, read each item's name and price, the item total, the tax, and the total.
7. Click Cancel. No order is placed.

**Returns:** each item's name and price, the item total, the tax, and the total.

**Success:** the review page showed every requested product, and all of the above has been read.

**Failure (a legitimate "no"):** the site rejects the login (for example the account is locked
out) — report "login rejected" with the reason the site shows; or a requested product isn't in
the catalog — report "product not found" with its name.

**Error (escalate to a human):** the checkout sends you back to the cart instead of the review
page, a page never finishes loading even after one retry, or anything else doesn't look like
what's described above. Stop, report
which step, what we expected, and what we actually saw, with a screenshot, and hand it to a
human.

---

## Place order

**You're given:** the same as Total with tax: a username, a password, a list of product names
(exactly as they appear in the catalog, each at most once), and the customer's first name,
last name and zip code (an existing 5-digit US zip code).

**What to do:**
1. Go to https://www.saucedemo.com and log in with the username and password.
2. Wait for the Products page to finish loading.
3. For each product in the list, click the "Add to cart" button on that product.
4. Open the cart, and click Checkout.
5. Enter the customer's first name, last name and zip code, and click Continue.
6. On the review page, read each item's name and price, the item total, the tax, and the total.
7. Click Finish. This places the order, so it always needs a human's approval first.
8. On the thank-you page, read the confirmation message. Don't download the PDF.

(Steps 1 to 6 are the same as in Total with tax; they're repeated here because each procedure
is given to the discovery agent on its own.)

**Returns:** each item's name and price, the item total, the tax, the total, and the
confirmation message.

**Success:** the thank-you page is showing after Finish.

**Failure (a legitimate "no"):** the site rejects the login (for example the account is locked
out) — report "login rejected" with the reason the site shows; or a requested product isn't in
the catalog — report "product not found" with its name.

**Error (escalate to a human):** as in Total with tax. If the human doesn't approve placing the
order, stop without placing it.

---

## Request a statement (mock legacy app)

Not SauceDemo. This one runs against `scripts/mock_legacy_app.py`, a small back-office
screen with table markup, no test ids, and two runtime conditions the public demo site
never produces: a notice that appears on some loads and covers the form, and (on demand)
a renamed control that breaks the recording. It exists so all three error classes can be
shown end to end. See `decisions.md` ADR-023.

**You're given:** a member number.

**What to do:**
1. Go to the app's home page.
2. A maintenance notice sometimes covers the form. If one is showing, dismiss it first.
   It won't always be there.
3. Enter the member number and click "Look up".
4. Read the member's status and balance.
5. Click "Request statement".

**Returns:** the member's status and balance.

**Success:** the "Statement requested" page is showing.

**Failure (a legitimate "no"):** none.

**Error (escalate to a human):** the member page never appears, the "Request statement"
control isn't on the page, or anything else doesn't look like what's described above.
Stop, report which step, what we expected, and what we actually saw, with a screenshot.
