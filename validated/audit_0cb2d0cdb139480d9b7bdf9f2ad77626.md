### Title
Minting Base Tokens (ETH) to `address(0)` via `receive_base_tokens` Permanently Freezes Bridged Funds — (`engine/src/engine.rs`)

---

### Summary

The `receive_base_tokens` function in `engine/src/engine.rs`, called during the `ft_on_transfer` bridge flow, does not validate that the parsed recipient address is non-zero. A NEAR user can supply `"0000000000000000000000000000000000000000"` as the `msg` field in an `ft_transfer_call` to the ETH connector, causing bridged ETH to be minted permanently to `address(0)` on Aurora — an address no one controls — resulting in a permanent freeze of those funds.

---

### Finding Description

When a NEAR user bridges ETH to Aurora, the ETH connector calls `ft_on_transfer` on the Aurora engine. Because the predecessor is the connector account, the engine dispatches to `receive_base_tokens`:

```rust
// engine/src/contract_methods/connector.rs, lines 81-82
let result = if predecessor_account_id == get_connector_account_id(&io)? {
    engine.receive_base_tokens(&args)
```

Inside `receive_base_tokens`, the recipient address is parsed from `args.msg` via `FtTransferMessageData::try_from`, and the ETH is immediately credited to that address with no zero-address guard:

```rust
// engine/src/engine.rs, lines 777-785
let message_data = FtTransferMessageData::try_from(args.msg.as_str())?;
let amount = Wei::new_u128(args.amount.as_u128());
let receipient = message_data.recipient;
let balance = get_balance(&self.io, &receipient);
let new_balance = balance
    .checked_add(amount)
    .ok_or(errors::ERR_BALANCE_OVERFLOW)?;
set_balance(&mut self.io, &receipient, &new_balance);
```

The parser in `FtTransferMessageData::try_from` accepts any 40-hex-character string, including `"0000000000000000000000000000000000000000"`, and returns `Address::from_array([0u8; 20])` without error:

```rust
// engine-types/src/parameters/connector.rs, lines 41-56
if message.len() == 40 {
    let mut address_bytes = [0; 20];
    hex::decode_to_slice(message, &mut address_bytes)
        .map_err(|_| errors::ParseOnTransferMessageError::InvalidHexData)?;
    Address::from_array(address_bytes)
    // ← no check: address_bytes != [0u8; 20]
```

There is no guard anywhere in this path that rejects `address(0)` as a recipient.

By contrast, the ERC-20 path (`receive_erc20_tokens`) is incidentally protected because OpenZeppelin's `_mint` in `EvmErc20.sol` reverts on `address(0)`:

```solidity
// etc/eth-contracts/contracts/EvmErc20.sol, line 49-51
function mint(address account, uint256 amount) public onlyAdmin {
    _mint(account, amount);  // OZ _mint reverts if account == address(0)
}
```

No equivalent protection exists for the base-token (ETH) path.

---

### Impact Explanation

**Critical — Permanent freezing of funds.**

When ETH is minted to `address(0)` in Aurora's internal state, it is irrecoverable:
- `address(0)` has no private key; no one can sign a transaction from it.
- The ETH cannot be withdrawn back to NEAR via the exit precompile because the caller would need to be `address(0)`.
- The ETH connector on NEAR has already transferred the NEP-141 ETH to Aurora, so the NEAR-side balance is debited.
- The funds are permanently locked in Aurora's key-value store at the zero-address balance slot.

---

### Likelihood Explanation

**Medium.** The `ft_transfer_call` entry point is callable by any NEAR account that holds ETH-connector NEP-141 tokens. The `msg` field is a free-form string. A user who mistypes or zero-initializes the recipient address (e.g., a buggy frontend, a script error, or a deliberate griefing attempt) will trigger this path. No special privilege is required.

---

### Recommendation

Add a zero-address check in `receive_base_tokens` immediately after parsing the recipient:

```rust
let receipient = message_data.recipient;
if receipient == Address::zero() {
    return Err(errors::ERR_INVALID_RECIPIENT);
}
```

Optionally, add the same guard inside `FtTransferMessageData::try_from` so that all callers benefit from the protection centrally.

---

### Proof of Concept

**Attacker-controlled entry path:**

1. Attacker holds NEP-141 ETH tokens on NEAR (obtained by bridging ETH from Ethereum, or by any other means).
2. Attacker calls `ft_transfer_call` on the ETH connector contract:
   ```json
   {
     "receiver_id": "aurora",
     "amount": "1000000000000000000",
     "msg": "0000000000000000000000000000000000000000"
   }
   ```
3. The ETH connector transfers the NEP-141 balance and calls `ft_on_transfer` on Aurora with `predecessor = eth_connector_account_id`.
4. `ft_on_transfer` dispatches to `receive_base_tokens` because `predecessor_account_id == get_connector_account_id(&io)?`.
5. `FtTransferMessageData::try_from("0000000000000000000000000000000000000000")` succeeds, returning `recipient = Address::zero()`.
6. `set_balance(&mut self.io, &Address::zero(), &new_balance)` is called — 1 ETH is credited to `address(0)`.
7. The ETH is permanently frozen. The attacker's NEAR-side balance is debited. No recovery is possible.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** engine/src/engine.rs (L773-789)
```rust
    pub fn receive_base_tokens(
        &mut self,
        args: &FtOnTransferArgs,
    ) -> Result<Option<SubmitResult>, ContractError> {
        let message_data = FtTransferMessageData::try_from(args.msg.as_str())?;
        let amount = Wei::new_u128(args.amount.as_u128());
        let receipient = message_data.recipient;
        let balance = get_balance(&self.io, &receipient);
        let new_balance = balance
            .checked_add(amount)
            .ok_or(errors::ERR_BALANCE_OVERFLOW)?;

        set_balance(&mut self.io, &receipient, &new_balance);

        sdk::log!("Mint {amount} base tokens for: {}", receipient.encode());

        Ok(None)
```

**File:** engine/src/contract_methods/connector.rs (L81-90)
```rust
        let result = if predecessor_account_id == get_connector_account_id(&io)? {
            engine.receive_base_tokens(&args)
        } else {
            engine.receive_erc20_tokens(
                &predecessor_account_id,
                &args,
                &current_account_id,
                handler,
            )
        };
```

**File:** engine-types/src/parameters/connector.rs (L40-56)
```rust
    fn try_from(message: &str) -> Result<Self, Self::Error> {
        if message.len() == 40 {
            // Parse message to determine recipient
            let recipient = {
                // Message format:
                // Recipient of the transaction - 40 characters (Address in hex)
                let mut address_bytes = [0; 20];
                hex::decode_to_slice(message, &mut address_bytes)
                    .map_err(|_| errors::ParseOnTransferMessageError::InvalidHexData)?;
                Address::from_array(address_bytes)
            };

            #[allow(deprecated)]
            return Ok(Self {
                recipient,
                fee: None,
            });
```

**File:** etc/eth-contracts/contracts/EvmErc20.sol (L49-51)
```text
    function mint(address account, uint256 amount) public onlyAdmin {
        _mint(account, amount);
    }
```
