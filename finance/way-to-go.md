# Way to Go, 18.09.2026

## File Structure

The finance folder is 

FINANCE_FOLDER = Path("/Users/johannes.siedersleben/Documents/finance")

for the real thing 

and 

FINANCE_FOLDER = Path("/Users/johannes.siedersleben/Documents/finance-test")

for testing (finance-test is a copy of finance).
The vorschlag files are to be stored in "finance/suggestions" or "finance-test/suggestions". 

The  test environment allows us to replay the last few years.

todo:
All programs need a toggle "test". 
The "vorschlag" files are to be stored in "finance/suggestions" or "finance-test/suggestions".  
The suffix should be "suggestion" rather than "vorschlag".

## build_rules
Input: money.csv, and a range (e.g. 36 month)
Output: a set of rules (json)

step 1: take "Verwendungszweck" into account.

step 2 (later on): generalize the rules to arbitrary predicates based on whatever logic, 
depending on external source e.g. Amazon mails.

My point: Amazon sends emails regarding orders, dispatch and delivery. 
The logic that links an Amazon booking to a given order is tricky. 
I'd like to be prepared for whatever algorithm we may find.

## categorize_import

input: a new umsatzlist

output: an import file that matches exactly the structure of money.csv, with KAT, KAT, BEM filled in
according to the rules.

I'll edit this file with Excel and export the result to csv.

## import_dkb

input: a new import file as produced by categorize_imports

output: money.csv with the input added twice (present and future)

The new input structure produced by categorize_imports matches exactly the structure of money.csv.
The input has the fields KAT, KAT, BEM filled in.
As to dedup: Stick for the time being to comparing figures; remove the bug (replace the set with a list).
