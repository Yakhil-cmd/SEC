### Title
Rebasing NEP-141 Token Accounting Mismatch Causes ERC-20 Mirror Insolvency and Permanent Fund Freeze - (`engine/src/engine.rs`, `engine-precompiles/src/native.rs`, `etc/eth-contracts/contracts/EvmErc20.sol`)

---

### Summary

Aurora Engine's NEP-141 ↔ ERC-20 bridge mints and redeems a fixed nominal token amount at every step. When the bridged NEP-141 token is a rebasing token (one whose account balances change autonomously without explicit transfers), the ERC-20 mirror supply diverges from the actual NEP-141 balance held by the Aurora contract. A negative rebase makes the ERC-20 undercollateralised: the last holders to exit cannot redeem their tokens, and the ERC-20 total supply permanently exceeds the NEP-141 reserves. A positive rebase silently transfers the surplus yield to the first users who exit, at the expense of remaining holders.

---

### Finding Description

**Deposit path — `receive_erc20_tokens`**

When a user bridges a NEP-141 token to Aurora via `ft_transfer_call`, the NEP-141 contract calls `ft_on_transfer` on Aurora. Aurora's engine dispatches to `receive_erc20_tokens`:

```rust
// engine/src/engine.rs  line 803
let amount = args.amount.as_u128();
// ...
setup_receive_erc20_tokens_input(&recipient, amount)  // mints exactly `amount` ERC-20
```

`setup_receive_erc20_tokens_input` encodes a call to `EvmErc20.mint(recipient, amount)`, minting exactly the nominal amount reported by the NEP-141 transfer. [1](#0-0) 

**Withdrawal path — `EvmErc20.withdrawToNear` → `exit_erc20_token_to_near`**

When a user exits, `EvmErc20.withdrawToNear` burns the caller's tokens and passes the burned amount to the `ExitToNear` precompile:

```solidity
// etc/eth-contracts/contracts/EvmErc20.sol  line 53-62
function withdrawToNear(bytes memory recipient, uint256 amount) external override {
    _burn(_msgSender(), amount);
    bytes32 amount_b = bytes32(amount);
    bytes memory input = abi.encodePacked("\x01", amount_b, recipient);
    // calls ExitToNear precompile with the burned amount
}
``` [2](#0-1) 

The precompile's `exit_erc20_token_to_near` then schedules an `ft_transfer` on the NEP-141 contract for exactly that same nominal amount:

```rust
// engine-precompiles/src/native.rs  line 632-636
format!(
    r#"{{"receiver_id":"{}","amount":"{}"}}"#,
    exit_params.receiver_account_id,
    exit_params.amount.as_u128()   // ← fixed nominal amount, not a share
),
"ft_transfer",
``` [3](#0-2) 

**The invariant that breaks for rebasing tokens**

The entire bridge assumes a strict 1:1 invariant:

> `ERC-20 total supply` == `NEP-141 balance held by Aurora`

For a rebasing NEP-141 token, the NEP-141 balance of Aurora's account changes autonomously (e.g., staking rewards accrue or a slashing event reduces it) without any `ft_transfer` being issued. The ERC-20 total supply is never adjusted to match, so the invariant silently breaks.

---

### Impact Explanation

**Negative rebase (e.g., slashing):**

1. Alice deposits 100 rebasing NEP-141 → Aurora mints 100 ERC-20 to Alice.
2. Bob deposits 100 rebasing NEP-141 → Aurora mints 100 ERC-20 to Bob.
3. Aurora's NEP-141 balance is 200; ERC-20 total supply is 200.
4. A 10 % negative rebase occurs → Aurora's NEP-141 balance drops to 180; ERC-20 supply stays at 200.
5. Alice calls `withdrawToNear(100)` → burns 100 ERC-20, schedules `ft_transfer(amount: 100)` → succeeds; Alice receives 100 NEP-141.
6. Bob calls `withdrawToNear(100)` → burns 100 ERC-20, schedules `ft_transfer(amount: 100)` → **fails**; Aurora only holds 80 NEP-141.

Bob's 100 ERC-20 tokens are permanently burned with no corresponding NEP-141 redeemable. This is a **permanent fund freeze** and **insolvency**: ERC-20 supply (0 after both burns) is backed by only 80 NEP-141, but Bob received nothing.

**Positive rebase (e.g., staking yield):**

Alice exits first and receives 100 NEP-141 even though Aurora's balance grew to 110. The 10-token surplus that belonged proportionally to both Alice and Bob is entirely captured by Alice. Bob exits and receives only 10 NEP-141 instead of 55. This is **theft of unclaimed yield** from Bob.

---

### Likelihood Explanation

- The Aurora bridge is **permissionless**: any NEP-141 token can be bridged by calling `deploy_erc20_token`. No admin approval is required.
- Rebasing NEP-141 tokens exist in the NEAR ecosystem (liquid staking derivatives such as stNEAR and similar tokens implement balance rebasing).
- The rebase event is a normal, automatic operation of the token contract — no attacker action is required beyond the initial deposit. Any user who deposits a rebasing NEP-141 and later exits is exposed.
- A sophisticated actor can front-run a known positive rebase to extract disproportionate yield, or simply exit before others after a negative rebase to avoid the loss entirely.

---

### Recommendation

Replace the fixed-amount accounting with a **shares-based** model for rebasing NEP-141 tokens, analogous to the mitigation described in the reference report:

1. **On deposit** (`receive_erc20_tokens`): instead of minting `args.amount` ERC-20 tokens, query the NEP-141 contract for the share count corresponding to `args.amount` and mint that many ERC-20 tokens (representing shares, not nominal balances).

2. **On withdrawal** (`exit_erc20_token_to_near` / `EvmErc20.withdrawToNear`): convert the ERC-20 share amount back to a current nominal NEP-141 amount at redemption time, and call `ft_transfer` with that converted amount.

3. Alternatively, restrict the bridge to non-rebasing NEP-141 tokens by adding an explicit check or registry, and document this limitation clearly.

---

### Proof of Concept

The following scenario demonstrates the insolvency path:

1. Deploy a rebasing NEP-141 token `rebase.near` on NEAR.
2. Call `deploy_erc20_token` on Aurora to create the ERC-20 mirror.
3. Alice calls `ft_transfer_call` on `rebase.near`, transferring 100 tokens to Aurora with `msg = alice_evm_address`. Aurora mints 100 ERC-20 to Alice.
4. Bob calls `ft_transfer_call`, transferring 100 tokens to Aurora. Aurora mints 100 ERC-20 to Bob. Aurora's NEP-141 balance = 200; ERC-20 supply = 200.
5. `rebase.near` performs a 10 % negative rebase. Aurora's NEP-141 balance drops to 180 without any transfer event. ERC-20 supply remains 200.
6. Alice calls `EvmErc20.withdrawToNear(alice_near_account, 100)`. The ERC-20 contract burns 100 tokens and the `ExitToNear` precompile schedules `ft_transfer({receiver_id: alice_near_account, amount: "100"})` on `rebase.near`. This succeeds; Alice receives 100 NEP-141.
7. Bob calls `EvmErc20.withdrawToNear(bob_near_account, 100)`. The ERC-20 contract burns 100 tokens and the precompile schedules `ft_transfer({receiver_id: bob_near_account, amount: "100"})`. This **panics** on `rebase.near` because Aurora only holds 80 NEP-141.
8. Bob's 100 ERC-20 tokens are permanently destroyed with no NEP-141 received. The ERC-20 is insolvent.

The entry path is entirely unprivileged: any token holder can trigger steps 3–7 using standard bridge interfaces (`ft_transfer_call` on NEAR and `withdrawToNear` on the ERC-20 contract).

### Citations

**File:** engine/src/engine.rs (L803-831)
```rust
        let amount = args.amount.as_u128();
        // Parse message to determine recipient
        let mut recipient = {
            // The message should contain the recipient EOA address.
            let message = args.msg.strip_prefix("0x").unwrap_or(&args.msg);
            // Recipient - 40 characters (Address in hex without '0x' prefix)
            if message.len() < 40 {
                return Err(ParseOnTransferMessageError::WrongMessageFormat.into());
            }
            let mut address_bytes = [0; 20];
            hex::decode_to_slice(&message[..40], &mut address_bytes)
                .map_err(|_| ParseOnTransferMessageError::WrongMessageFormat)?;
            Address::from_array(address_bytes)
        };

        if let Some(fallback_address) = silo::get_erc20_fallback_address(&self.io)
            && !silo::is_allow_receive_erc20_tokens(&self.io, &recipient)
        {
            recipient = fallback_address;
        }

        let erc20_token = get_erc20_from_nep141(&self.io, token)?;
        let erc20_admin_address = current_address(current_account_id);
        let result = self
            .call(
                &erc20_admin_address,
                &erc20_token,
                Wei::zero(),
                setup_receive_erc20_tokens_input(&recipient, amount),
```

**File:** etc/eth-contracts/contracts/EvmErc20.sol (L53-63)
```text
    function withdrawToNear(bytes memory recipient, uint256 amount) external override {
        _burn(_msgSender(), amount);

        bytes32 amount_b = bytes32(amount);
        bytes memory input = abi.encodePacked("\x01", amount_b, recipient);
        uint input_size = 1 + 32 + recipient.length;

        assembly {
            let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
        }
    }
```

**File:** engine-precompiles/src/native.rs (L630-646)
```rust
            (
                nep141_account_id,
                format!(
                    r#"{{"receiver_id":"{}","amount":"{}"}}"#,
                    exit_params.receiver_account_id,
                    exit_params.amount.as_u128()
                ),
                "ft_transfer",
                None,
                events::ExitToNear::Legacy(ExitToNearLegacy {
                    sender: Address::new(erc20_address),
                    erc20_address: Address::new(erc20_address),
                    dest: exit_params.receiver_account_id.to_string(),
                    amount: exit_params.amount,
                }),
            )
        }
```
