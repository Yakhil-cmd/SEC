### Title
Permissionless Third-Party Participation Registration Bypasses Confirmation Text in SNS Swap `refresh_buyer_tokens` - (File: rs/sns/swap/canister/canister.rs)

---

### Summary

The SNS Swap canister's `refresh_buyer_tokens` endpoint accepts an arbitrary `buyer` principal from any unprivileged caller with no check that the caller matches the buyer. Because the `confirmation_text` (when required) is a publicly readable value set at SNS initialization, any third party can register a victim's ICP participation without the victim's explicit consent, bypassing the confirmation text mechanism entirely. This is the IC analog of the permissionless `matchOrders()` front-running class: a publicly callable function that operates on another party's assets without their authorization.

---

### Finding Description

In `rs/sns/swap/canister/canister.rs`, the `refresh_buyer_tokens` update handler resolves the buyer identity as follows:

```rust
let p: PrincipalId = if arg.buyer.is_empty() {
    caller_principal_id()
} else {
    PrincipalId::from_str(&arg.buyer).unwrap()
};
``` [1](#0-0) 

There is **no check** that `caller_principal_id() == p` when `arg.buyer` is non-empty. Any unprivileged ingress sender can supply an arbitrary buyer principal.

The underlying `refresh_buyer_token_e8s` in `rs/sns/swap/src/swap.rs` then:
1. Reads the ICP balance from the ledger for the specified buyer's subaccount.
2. Validates `confirmation_text` against the SNS-specified text — but the caller supplies this text, not the buyer.
3. Registers the buyer's participation and locks their ICP in the swap. [2](#0-1) 

The `confirmation_text` is set during SNS initialization and is publicly readable via the `get_sale_parameters` query endpoint. The proto definition explicitly documents that the buyer field is optional and defaults to the caller only when empty: [3](#0-2) 

The confirmation text validation only checks that the caller-supplied string matches the SNS-configured string — it does not verify that the confirmation came from the buyer: [4](#0-3) 

---

### Impact Explanation

**1. Confirmation text mechanism rendered ineffective.** The `confirmation_text` field exists to ensure participants explicitly acknowledge legal or regulatory terms before their ICP is committed. Because the text is public and any caller can supply it on behalf of any buyer, the mechanism provides no real protection. A victim's ICP can be locked into a swap without the victim ever having acknowledged the terms.

**2. Unauthorized ICP locking.** Once `refresh_buyer_token_e8s` succeeds for a victim, their ICP is committed to the swap. The victim cannot recover it until the swap ends (committed or aborted) by calling `error_refund_icp`. If the victim changed their mind after transferring ICP but before calling `refresh_buyer_tokens`, a third party can force their participation. [5](#0-4) 

**3. Swap capacity ordering attack.** When a swap is near its maximum ICP target, the order of `refresh_buyer_tokens` calls determines who participates. An attacker who monitors the ICP ledger can observe a victim's transfer and immediately call `refresh_buyer_tokens` for a different buyer (one whose participation consumes the remaining capacity), causing the victim's subsequent call to fail with "ICP target already reached." [6](#0-5) 

---

### Likelihood Explanation

**Medium.** The ICP ledger is public; any observer can detect when a principal transfers ICP to a swap subaccount. The confirmation text is readable via `get_sale_parameters`. The attacker only needs to submit an ingress message before the victim's message is ordered into a block. On the IC, there is no gas auction, but an attacker with a low-latency boundary node connection can reliably race a victim's message. The attack requires no privileged access, no key material, and no threshold corruption.

---

### Recommendation

1. **Enforce caller == buyer**: In `refresh_buyer_tokens`, reject calls where `arg.buyer` is non-empty and does not match `caller_principal_id()`. If intentional third-party registration is needed, scope it to a specific allowlist (e.g., the NNS governance canister).
2. **Redesign confirmation text**: If the confirmation text has legal significance, it must be tied to the buyer's identity — for example, by requiring the buyer's ingress signature on the text rather than accepting it as a plain string from any caller.

---

### Proof of Concept

1. Alice transfers 10 ICP to her swap subaccount (`principal_to_subaccount(alice)` on the swap canister).
2. The SNS swap has `confirmation_text = "I agree to the terms"` (readable via `get_sale_parameters`).
3. Attacker submits an ingress call to `refresh_buyer_tokens` with:
   - `buyer = alice_principal_text`
   - `confirmation_text = Some("I agree to the terms")`
4. The swap canister reads Alice's ledger balance (10 ICP), validates the confirmation text (matches), and registers Alice's participation — all without Alice having called anything.
5. Alice's 10 ICP is now locked in the swap. If Alice calls `refresh_buyer_tokens` herself, she receives the already-registered result. If she never intended to participate (e.g., she transferred ICP by mistake), she must wait for the swap to end to reclaim her funds via `error_refund_icp`. [1](#0-0) [7](#0-6)

### Citations

**File:** rs/sns/swap/canister/canister.rs (L128-143)
```rust
async fn refresh_buyer_tokens(arg: RefreshBuyerTokensRequest) -> RefreshBuyerTokensResponse {
    log!(INFO, "refresh_buyer_tokens");
    let p: PrincipalId = if arg.buyer.is_empty() {
        caller_principal_id()
    } else {
        PrincipalId::from_str(&arg.buyer).unwrap()
    };
    let icp_ledger = create_real_icp_ledger(swap().init_or_panic().icp_ledger_or_panic());
    match swap_mut()
        .refresh_buyer_token_e8s(p, arg.confirmation_text, this_canister_id(), &icp_ledger)
        .await
    {
        Ok(r) => r,
        Err(msg) => panic!("{}", msg),
    }
}
```

**File:** rs/sns/swap/src/swap.rs (L1134-1171)
```rust
    pub async fn refresh_buyer_token_e8s(
        &mut self,
        buyer: PrincipalId,
        confirmation_text: Option<String>,
        this_canister: CanisterId,
        icp_ledger: &dyn ICRC1Ledger,
    ) -> Result<RefreshBuyerTokensResponse, String> {
        use swap_participation::*;

        // These two checks need to be repeated after awaiting the response from the ICP ledger.
        self.validate_lifecycle_is_open()
            .map_err(context_before_awaiting_icp_ledger_response)?;
        self.validate_possibility_of_direct_participation()
            .map_err(context_before_awaiting_icp_ledger_response)?;

        // User input validation doesn't expire after await, so this check doesn't need repetition.
        self.validate_confirmation_text(confirmation_text)?;

        // Look for the token balance of the specified principal's subaccount on 'this' canister.
        let e8s = {
            let account = Account {
                owner: this_canister.get().0,
                subaccount: Some(principal_to_subaccount(&buyer)),
            };
            icp_ledger
                .account_balance(account)
                .await
                .map_err(|x| x.to_string())?
                .get_e8s()
        };

        // Recheck lifecycle state and ICP target after async call because the swap could have
        // been closed (committed or aborted) while the call to get the account balance was
        // outstanding.
        self.validate_lifecycle_is_open()
            .map_err(context_after_awaiting_icp_ledger_response)?;
        self.validate_possibility_of_direct_participation()
            .map_err(context_after_awaiting_icp_ledger_response)?;
```

**File:** rs/sns/swap/src/swap.rs (L1285-1291)
```rust
        self.buyers
            .entry(buyer.to_string())
            .or_insert_with(|| BuyerState::new(0))
            .set_amount_icp_e8s(new_balance_e8s);
        // We compute the current participation amounts once and store the result in Swap's state,
        // for efficiency reasons.
        self.update_total_participation_amounts();
```

**File:** rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto (L843-851)
```text
message RefreshBuyerTokensRequest {
  // If not specified, the caller is used.
  string buyer = 1;

  // To accept the swap participation confirmation, a participant should send
  // the confirmation text via refresh_buyer_tokens, matching the text set
  // during SNS initialization.
  optional string confirmation_text = 2;
}
```
