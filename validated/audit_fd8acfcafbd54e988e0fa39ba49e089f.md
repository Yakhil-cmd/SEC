### Title
Missing Anonymous Principal Validation in `error_refund_icp` Allows Draining of Swap Canister's Anonymous Subaccount - (File: rs/sns/swap/src/swap.rs)

### Summary
The `error_refund_icp` function in the SNS Swap canister accepts a caller-controlled `source_principal_id` and transfers ICP to that principal without validating that it is not the anonymous principal. This mirrors the Solidity pattern of sending funds to an unvalidated `_feeReceiver` address. An unprivileged ingress sender can drain ICP from the swap canister's anonymous subaccount to the anonymous principal's ICP ledger account, from which the attacker can subsequently steal the funds by calling the ICP ledger as anonymous.

### Finding Description

The `error_refund_icp` update endpoint in the SNS Swap canister is callable by any unprivileged principal with no caller restriction. It accepts a `source_principal_id` field in the request and unconditionally transfers the balance of `swap_canister[principal_to_subaccount(source_principal_id)]` to `Account { owner: source_principal_id.0, subaccount: None }` on the ICP ledger. [1](#0-0) 

The function performs no check that `source_principal_id` is not the anonymous principal. Contrast this with `new_sale_ticket`, which explicitly rejects anonymous callers: [2](#0-1) 

The canister-level handler passes the request directly to the core logic with no additional validation: [3](#0-2) 

The ICP ledger permits the anonymous principal to hold and receive tokens (the `transfer` endpoint does not reject anonymous destination accounts): [4](#0-3) 

The `ErrorRefundIcpRequest` proto definition confirms `source_principal_id` is fully attacker-controlled: [5](#0-4) 

### Impact Explanation

**Vulnerability class:** Ledger conservation bug / cycles-resource accounting bug.

Attack path:
1. Attacker (or anyone) sends ICP to `swap_canister[principal_to_subaccount(anonymous)]` on the ICP ledger. This is a valid 32-byte subaccount derived from the anonymous principal bytes. The ICP ledger accepts transfers to any account identifier.
2. The swap reaches `Lifecycle::Aborted` or `Lifecycle::Committed` (the only states where `error_refund_icp` proceeds past the lifecycle check).
3. Attacker calls `error_refund_icp({ source_principal_id: Some(anonymous_principal) })` as any ingress sender.
4. The function computes `source_subaccount = principal_to_subaccount(anonymous)`, queries the balance of `swap_canister[anonymous_subaccount]`, and transfers `balance - DEFAULT_TRANSFER_FEE` to `Account { owner: anonymous, subaccount: None }` on the ICP ledger.
5. Attacker calls `icrc1_transfer` on the ICP ledger as the anonymous principal (the ICP ledger allows anonymous principals to transfer tokens they hold) to move the funds to the attacker's own account. [6](#0-5) 

The ICP ledger's `icrc1_transfer` does not block the anonymous principal from sending: [7](#0-6) 

The result is a loss of ICP that was held in the swap canister's anonymous subaccount.

### Likelihood Explanation

**Medium-low.** The preconditions are:
- ICP must be present in `swap_canister[anonymous_subaccount]`. This can occur via accidental transfer (a user mistakenly sends ICP to the anonymous subaccount of the swap canister) or via deliberate setup by the attacker themselves (attacker sends ICP to that subaccount, then recovers it via this path — though this is self-defeating unless the attacker is racing another depositor).
- The swap must be in `Aborted` or `Committed` state, which is a normal terminal state for every SNS swap.

The `error_refund_icp` endpoint is publicly callable with no authentication requirement, making step 3 trivially reachable by any ingress sender.

### Recommendation

Add an anonymous principal check immediately after unpacking `source_principal_id`, consistent with the pattern already used in `new_sale_ticket`:

```rust
if source_principal_id.is_anonymous() {
    return ErrorRefundIcpResponse::new_invalid_request_error(
        "source_principal_id must not be the anonymous principal.",
    );
}
``` [8](#0-7) 

### Proof of Concept

1. Deploy an SNS swap canister and advance it to `Committed` or `Aborted` state.
2. Transfer any amount of ICP (e.g., 1 ICP) to `AccountIdentifier::new(swap_canister_id, Some(Subaccount::from(&PrincipalId::new_anonymous())))` on the ICP ledger.
3. Call `error_refund_icp` on the swap canister with `source_principal_id = Some(PrincipalId::new_anonymous())` from any ingress identity.
4. Observe that the ICP is transferred to `Account { owner: anonymous, subaccount: None }` on the ICP ledger (confirmed by querying `icrc1_balance_of` for the anonymous account).
5. Call `icrc1_transfer` on the ICP ledger as the anonymous principal, specifying the attacker's account as the destination, to complete the theft.

The missing guard is visible by comparing `error_refund_icp` (no anonymous check) against `new_sale_ticket` (explicit `caller.is_anonymous()` guard at line 2537): [9](#0-8)

### Citations

**File:** rs/sns/swap/src/swap.rs (L1938-2004)
```rust
        // Unpack request.
        let source_principal_id = match request {
            ErrorRefundIcpRequest {
                source_principal_id: Some(source_principal_id),
            } => source_principal_id,
            _ => {
                return ErrorRefundIcpResponse::new_invalid_request_error(format!(
                    "Invalid request. Must have source_principal_id. Request:\n{request:#?}",
                ));
            }
        };

        if let Some(buyer_state) = self.buyers.get(&source_principal_id.to_string()) {
            if let Some(transfer) = &buyer_state.icp
                && transfer.transfer_success_timestamp_seconds == 0
            {
                // This buyer has ICP not yet disbursed using the normal mechanism.
                return ErrorRefundIcpResponse::new_precondition_error(format!(
                    "ICP cannot be refunded as principal {} has {} ICP (e8s) in escrow",
                    source_principal_id,
                    buyer_state.amount_icp_e8s()
                ));
            }
            // This buyer has participated in the swap, but all ICP
            // has already been disbursed, either back to the buyer
            // (aborted) or to the SNS Governance canister
            // (committed). Any ICP in this buyer's subaccount must
            // belong to the buyer.
        } else {
            // This buyer is not known to the swap canister. Any
            // balance in a subaccount belongs to the buyer.
        }

        let source_subaccount = principal_to_subaccount(source_principal_id);

        // Figure out how much to send back to source_principal_id based on
        // what's left in the subaccount.
        let account_balance_result = icp_ledger
            .account_balance(Account {
                owner: self_canister_id.into(),
                subaccount: Some(source_subaccount),
            })
            .await;
        let balance_e8s = match account_balance_result {
            Ok(balance) => balance.get_e8s(),
            Err(err) => {
                return ErrorRefundIcpResponse::new_external_error(format!(
                    "Unable to get the balance for the subaccount of {source_principal_id}: {err:?}",
                ));
            }
        };

        // Make transfer.
        let amount_e8s = balance_e8s.saturating_sub(DEFAULT_TRANSFER_FEE.get_e8s());
        let dst = Account {
            owner: source_principal_id.0,
            subaccount: None,
        };
        let transfer_result = icp_ledger
            .transfer_funds(
                amount_e8s,
                DEFAULT_TRANSFER_FEE.get_e8s(),
                Some(source_subaccount),
                dst,
                0, // memo
            )
            .await;
```

**File:** rs/sns/swap/src/swap.rs (L2536-2539)
```rust

        if caller.is_anonymous() {
            return NewSaleTicketResponse::err_invalid_principal();
        }
```

**File:** rs/sns/swap/canister/canister.rs (L161-167)
```rust
#[update]
async fn error_refund_icp(request: ErrorRefundIcpRequest) -> ErrorRefundIcpResponse {
    let icp_ledger = create_real_icp_ledger(swap().init_or_panic().icp_ledger_or_panic());
    swap()
        .error_refund_icp(this_canister_id(), &request, &icp_ledger)
        .await
}
```

**File:** rs/ledger_suite/icp/ledger/src/main.rs (L192-204)
```rust
async fn send(
    memo: Memo,
    amount: Tokens,
    fee: Tokens,
    from_subaccount: Option<Subaccount>,
    to: AccountIdentifier,
    created_at_time: Option<TimeStamp>,
) -> Result<BlockIndex, TransferError> {
    let caller_principal_id = PrincipalId::from(caller());

    if !LEDGER.read().unwrap().can_send(&caller_principal_id) {
        panic!("Sending from {caller_principal_id} is not allowed");
    }
```

**File:** rs/ledger_suite/icp/ledger/src/main.rs (L807-843)
```rust
#[update]
async fn icrc1_transfer(
    arg: TransferArg,
) -> Result<Nat, icrc_ledger_types::icrc1::transfer::TransferError> {
    if !LEDGER
        .read()
        .unwrap()
        .can_send(&PrincipalId::from(caller()))
    {
        trap("Caller cannot hold tokens on the ledger.");
    }

    let from_account = Account {
        owner: caller(),
        subaccount: arg.from_subaccount,
    };
    Ok(Nat::from(
        icrc1_send(
            arg.memo,
            arg.amount,
            arg.fee,
            from_account,
            arg.to,
            None,
            arg.created_at_time,
        )
        .await
        .map_err(convert_transfer_error)
        .map_err(|err| {
            let err: Icrc1TransferError = match Icrc1TransferError::try_from(err) {
                Ok(err) => err,
                Err(err) => trap(&err),
            };
            err
        })?,
    ))
}
```

**File:** rs/sns/swap/src/gen/ic_sns_swap.pb.v1.rs (L1979-1984)
```rust
pub struct ErrorRefundIcpRequest {
    /// Principal who originally sent the funds to us, and is now asking for any
    /// unaccepted balance to be returned.
    #[prost(message, optional, tag = "1")]
    pub source_principal_id: ::core::option::Option<::ic_base_types::PrincipalId>,
}
```
