### Title
ICP Ledger Unconditionally Fills Deduplication Window for Every Transfer, Enabling Throttle-Based DoS and ICRC-2 Allowance Expiry — (File: rs/ledger_suite/icp/ledger/src/lib.rs)

---

### Summary

The ICP ledger's `add_payment_with_timestamp()` unconditionally sets `created_at_time` to `now` even when the caller passes `None`, causing **every**