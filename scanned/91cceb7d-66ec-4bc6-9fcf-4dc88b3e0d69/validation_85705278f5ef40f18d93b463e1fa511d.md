### Title
Missing Idempotency Guard in `settle_neurons_fund_participation` for Aborted Swaps Prevents Buyer Refunds - (File: `rs/sns/swap/src/swap.rs`)

### Summary
The `settle_neurons_fund_participation` function in the SNS Swap canister uses `!self.cf_participants.is_empty()` as its sole idempotency guard. This guard only fires for **committed** swaps where Neurons' Fund participants are non-empty. For **aborted** swaps, `cf_participants` is always empty, so the guard never triggers. On every sequential call to `finalize_swap`, the function re-calls NNS governance. NNS governance returns an error on the second call (its own state machine detects the already-settled state). That error halts `finalize_inner`, breaking the retry-ability of `finalize_swap` and permanently blocking buyers who were not yet refunded.

### Finding Description

`settle_neurons_fund_participation` in `rs/sns/swap/src/swap.rs` opens with a single idempotency check:

```rust
// Check if any work needs to be done.
if !