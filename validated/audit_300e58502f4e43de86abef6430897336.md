### Title
ckETH Minter Withdrawal Endpoints Lack ICRC-21 Human-Readable Consent Messages, Leaving ERC-20 `data` Field Opaque to Signers - (File: rs/ethereum/cketh/minter/cketh_minter.did)

### Summary
The ckETH minter canister exposes `withdraw_eth` and `withdraw_erc20` as publicly callable update endpoints but does not implement the ICRC-21 consent message standard. Unlike the ckBTC and ckDOGE minters, which both expose `icrc21_canister_call_consent_message` and decode withdrawal parameters into human-readable fields, the ckETH minter has no such endpoint. For ERC-20 withdrawals the actual recipient address and token amount are encoded as opaque ABI bytes in the EIP-1559 `data` field; without ICRC-21 there is no on-chain mechanism for a wallet or hardware device to surface those values to the user before the burn-and-send is committed.

### Finding Description

**Confirmed absence of ICRC-21 on the ckETH minter.**
The ckBTC minter DID exposes `icrc21_canister_call_consent_message` and is backed by a full implementation in `rs/bitcoin/ckbtc/minter/src/updates/icrc21.rs`. The ckDOGE minter mirrors this in `rs/dogecoin/ckdoge/minter/src/updates/icrc21.rs`. The ckETH minter source tree contains no `icrc21.rs` file and the service block of `rs/ethereum/cketh/minter/cketh_minter.did` ends at `decode_ledger_memo` with no `icrc21_canister_call_consent_message` entry. [1](#0-0) [2](#0-1) 

**The ERC-20 withdrawal path encodes the real recipient inside opaque `data` bytes.**
`create_transaction` in `rs/ethereum/cketh/minter/src/state/transactions/mod.rs` builds the EIP-1559 transaction for `withdraw_erc20` with `destination` set to the ERC-20 *contract* address, `amount` set to `Wei::ZERO`, and `data` set to ABI-encoded `transfer(address,uint256)` call data. The actual recipient address and token amount live entirely inside those bytes. [3](#0-2) [4](#0-3) 

**`withdraw_eth` and `withdraw_erc20` are directly callable by any non-anonymous principal.**
Both are `#[update]` endpoints that call `validate_caller_not_anonymous()` and immediately proceed to burn ckETH/ckERC-20 and queue an Ethereum transaction signed via threshold ECDSA. [5](#0-4) 

**The signing step uses a fixed derivation path with no user-visible address derivation.**
`Eip1559TransactionRequest::sign()` calls `sign_with_ecdsa` with `MAIN_DERIVATION_PATH` and returns the raw signature; no decoded Ethereum sender address is surfaced to the caller. [6](#0-5) 

### Impact Explanation
A user or hardware wallet (e.g., Ledger via the ICRC-21 flow) that calls `icrc21_canister_call_consent_message` on the ckETH minter receives a rejection because the endpoint does not exist. The wallet therefore cannot display:
- The ETH or ERC-20 amount being withdrawn.
- The Ethereum recipient address (for ERC-20 withdrawals this is buried in the `data` field).
- The token symbol.

A malicious dapp can present a misleading UI and invoke `withdraw_eth` or `withdraw_erc20` with an attacker-controlled `recipient` field. Because no ICRC-21 consent message is available, a hardware wallet or secure wallet has no standard path to interrupt and display the real parameters before the ckETH ledger burn is executed and the Ethereum transaction is queued. Once the burn succeeds the withdrawal is irreversible.

### Likelihood Explanation
The ckETH minter is a production canister on the IC mainnet actively used for ETH and ERC-20 withdrawals. The ICRC-21 standard is already adopted by the ckBTC minter, ckDOGE minter, ICP ledger, and ICRC-1 ledger, and hardware wallet integrations rely on it. Any user interacting with the ckETH minter through an ICRC-21-aware wallet is affected today. The ERC-20 case is the higher-risk variant because the visible `destination` field is the contract address, not the recipient, making the opaque `data` field the only place the real recipient appears.

### Recommendation
1. Add an `icrc21_canister_call_consent_message` endpoint to the ckETH minter, following the pattern in `rs/bitcoin/ckbtc/minter/src/updates/icrc21.rs`.
2. For `withdraw_eth`: display amount (in ETH), recipient Ethereum address, and token symbol.
3. For `withdraw_erc20`: decode `TransactionCallData::Erc20Transfer { to, value }` from the `data` field and display the decoded recipient address, token amount, and ERC-20 token symbol — not the contract address stored in `destination`.
4. Advertise ICRC-10 and ICRC-21 in `icrc10_supported_standards` once implemented.

### Proof of Concept
```
# Step 1 – confirm the endpoint is absent on the ckETH minter
dfx canister --network ic call <cketh_minter_id> icrc21_canister_call_consent_message \
  '(record { method = "withdraw_eth"; arg = blob "..."; user_preferences = record { metadata = record { language = "en"; utc_offset_minutes = null }; device_spec = null } })'
# → Canister has no update method 'icrc21_canister_call_consent_message'

# Step 2 – confirm the same call succeeds on the ckBTC minter
dfx canister --network ic call <ckbtc_minter_id> icrc21_canister_call_consent_message \
  '(record { method = "retrieve_btc_with_approval"; arg = blob "..."; user_preferences = record { metadata = record { language = "en"; utc_offset_minutes = null }; device_spec = null } })'
# → Ok (human-readable consent message with amount and BTC address)

# Step 3 – for withdraw_erc20, inspect the created transaction's data field
# The data field contains 68 bytes: 4-byte selector a9059cbb + 32-byte padded address + 32-byte amount
# Without ICRC-21, no wallet can surface the decoded recipient to the user before signing
```

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/updates/icrc21.rs (L65-114)
```rust
pub fn icrc21_canister_call_consent_message(
    consent_msg_request: ConsentMessageRequest,
) -> Result<ConsentInfo, Icrc21Error> {
    let network = read_state(|s| s.btc_network);
    build_consent_info(consent_msg_request, network)
}

pub(super) fn build_consent_info(
    consent_msg_request: ConsentMessageRequest,
    network: Network,
) -> Result<ConsentInfo, Icrc21Error> {
    if consent_msg_request.arg.len() > MAX_CONSENT_MESSAGE_ARG_SIZE_BYTES as usize {
        return Err(Icrc21Error::UnsupportedCanisterCall(ErrorInfo {
            description: format!(
                "The argument size is too large. The maximum allowed size is \
                 {MAX_CONSENT_MESSAGE_ARG_SIZE_BYTES} bytes."
            ),
        }));
    }

    let display_type = consent_msg_request
        .user_preferences
        .device_spec
        .clone()
        .unwrap_or(DisplayMessageType::GenericDisplay);

    let symbols = TokenSymbols::for_network(network);

    let consent_message = match consent_msg_request.method.as_str() {
        "retrieve_btc_with_approval" => {
            let args = Decode!(
                consent_msg_request.arg.as_slice(),
                RetrieveBtcWithApprovalArgs
            )
            .map_err(|e| {
                Icrc21Error::UnsupportedCanisterCall(ErrorInfo {
                    description: format!("Failed to decode RetrieveBtcWithApprovalArgs: {e}"),
                })
            })?;
            validate_address(&args.address, network)?;
            build_retrieve_btc_with_approval_message(&args, &display_type, symbols)
        }
        method => {
            return Err(Icrc21Error::UnsupportedCanisterCall(ErrorInfo {
                description: format!(
                    "The method '{method}' is not supported by the ckBTC minter ICRC-21 endpoint."
                ),
            }));
        }
    };
```

**File:** rs/ethereum/cketh/minter/cketh_minter.did (L696-750)
```text
service : (MinterArg) -> {
    // Retrieve the Ethereum address controlled by the minter:
    // * Deposits will be transferred from the helper smart contract to this address
    // * Withdrawals will originate from this address
    // IMPORTANT: Do NOT send ETH to this address directly. Use the helper smart contract instead so that the minter
    // knows to which IC principal the funds should be deposited.
    minter_address : () -> (text);

    // Address of the helper smart contract.
    // Returns "N/A" if the helper smart contract is not set.
    // IMPORTANT:
    // * Use this address to send ETH to the minter to convert it to ckETH.
    // * In case the smart contract needs to be updated the returned address will change!
    //   Always check the address before making a transfer.
    smart_contract_address : () -> (text) query;

    // Estimate the price of a transaction issued by the minter when converting ckETH to ETH.
    eip_1559_transaction_price : (opt Eip1559TransactionPriceArg) -> (Eip1559TransactionPrice) query;

    // Returns internal minter parameters
    get_minter_info : () -> (MinterInfo) query;

    // Withdraw the specified amount in Wei to the given Ethereum address.
    // IMPORTANT: The current gas limit is set to 21,000 for a transaction so withdrawals to smart contract addresses will likely fail.
    withdraw_eth : (WithdrawalArg) -> (variant { Ok : RetrieveEthRequest; Err : WithdrawalError });

    // Withdraw the specified amount of ERC-20 tokens to the given Ethereum address.
    withdraw_erc20 : (WithdrawErc20Arg) -> (variant { Ok : RetrieveErc20Request; Err : WithdrawErc20Error });

    // Retrieve the status of a Eth withdrawal request.
    retrieve_eth_status : (nat64) -> (RetrieveEthStatus);

    // Return details of all withdrawals matching the given search parameter.
    withdrawal_status : (WithdrawalSearchParameter) -> (vec WithdrawalDetail) query;

    // Check if an address is blocked by the minter.
    is_address_blocked : (text) -> (bool) query;

    // Retrieve the status of the minter canister.
    //
    // This is a debug endpoint where backwards-compatibility is not guaranteed.
    get_canister_status : () -> (CanisterStatusResponse);

    // Retrieve events from the minter's audit log.
    // The endpoint can return fewer events than requested to bound the response size.
    // IMPORTANT: this endpoint is meant as a debugging tool and is not guaranteed to be backwards-compatible.
    get_events : (record { start : nat64; length : nat64 }) -> (record { events : vec Event; total_event_count : nat64 }) query;

    // Add a ckERC-20 token to be supported by the minter.
    // This call is restricted to the orchestrator ID.
    add_ckerc20_token : (AddCkErc20Token) -> ();

    // Decode ledger memos produced by the minter when minting (deposits) or burning (withdrawals).
    decode_ledger_memo : (DecodeLedgerMemoArgs) -> (DecodeLedgerMemoResult) query;
}
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L1169-1184)
```rust
            Ok(Eip1559TransactionRequest {
                chain_id: ethereum_network.chain_id(),
                nonce,
                max_priority_fee_per_gas: gas_fee_estimate.max_priority_fee_per_gas,
                max_fee_per_gas: request_max_fee_per_gas,
                gas_limit,
                destination: request.erc20_contract_address,
                amount: Wei::ZERO,
                data: TransactionCallData::Erc20Transfer {
                    to: request.destination,
                    value: request.withdrawal_amount,
                }
                .encode(),
                access_list: Default::default(),
            })
        }
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L1196-1232)
```rust
impl TransactionCallData {
    /// Encode the transaction call data to interact with an Ethereum smart contract.
    /// See the [Contract ABI Specification](https://docs.soliditylang.org/en/develop/abi-spec.html#contract-abi-specification).
    pub fn encode(&self) -> Vec<u8> {
        match self {
            TransactionCallData::Erc20Transfer { to, value } => {
                let mut data = Vec::with_capacity(68);
                data.extend(ERC_20_TRANSFER_FUNCTION_SELECTOR);
                data.extend(<[u8; 32]>::from(to));
                data.extend(value.to_be_bytes());
                data
            }
        }
    }

    pub fn decode<T: AsRef<[u8]>>(data: T) -> Result<Self, String> {
        let data = data.as_ref();
        match data.get(0..4) {
            Some(selector) if selector == ERC_20_TRANSFER_FUNCTION_SELECTOR => {
                if data.len() != 68 {
                    return Err("Invalid data length".to_string());
                }
                let address = <[u8; 32]>::try_from(&data[4..36]).unwrap();
                let to = Address::try_from(&address).unwrap();

                let value = <[u8; 32]>::try_from(&data[36..]).unwrap();
                let value = Erc20Value::from_be_bytes(value);

                Ok(TransactionCallData::Erc20Transfer { to, value })
            }
            Some(selector) => Err(format!(
                "Unknown function selector 0x{:?}",
                hex::encode(selector)
            )),
            None => Err("missing function selector".to_string()),
        }
    }
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L265-340)
```rust
#[update]
async fn withdraw_eth(
    WithdrawalArg {
        amount,
        recipient,
        from_subaccount,
    }: WithdrawalArg,
) -> Result<RetrieveEthRequest, WithdrawalError> {
    let caller = validate_caller_not_anonymous();
    let _guard = retrieve_withdraw_guard(caller).unwrap_or_else(|e| {
        ic_cdk::trap(format!(
            "Failed retrieving guard for principal {caller}: {e:?}"
        ))
    });

    let destination = validate_address_as_destination(&recipient).map_err(|e| match e {
        AddressValidationError::Invalid { .. } | AddressValidationError::NotSupported(_) => {
            ic_cdk::trap(e.to_string())
        }
        AddressValidationError::Blocked(address) => WithdrawalError::RecipientAddressBlocked {
            address: address.to_string(),
        },
    })?;

    let amount = Wei::try_from(amount).expect("failed to convert Nat to u256");

    let minimum_withdrawal_amount = read_state(|s| s.cketh_minimum_withdrawal_amount);
    if amount < minimum_withdrawal_amount {
        return Err(WithdrawalError::AmountTooLow {
            min_withdrawal_amount: minimum_withdrawal_amount.into(),
        });
    }

    let client = read_state(LedgerClient::cketh_ledger_from_state);
    let now = ic_cdk::api::time();
    log!(INFO, "[withdraw]: burning {:?}", amount);
    match client
        .burn_from(
            Account {
                owner: caller,
                subaccount: from_subaccount,
            },
            amount,
            BurnMemo::Convert {
                to_address: destination,
            },
        )
        .await
    {
        Ok(ledger_burn_index) => {
            let withdrawal_request = EthWithdrawalRequest {
                withdrawal_amount: amount,
                destination,
                ledger_burn_index,
                from: caller,
                from_subaccount: from_subaccount.and_then(LedgerSubaccount::from_bytes),
                created_at: Some(now),
            };

            log!(
                INFO,
                "[withdraw]: queuing withdrawal request {:?}",
                withdrawal_request,
            );

            mutate_state(|s| {
                process_event(
                    s,
                    EventType::AcceptedEthWithdrawalRequest(withdrawal_request.clone()),
                );
            });
            Ok(RetrieveEthRequest::from(withdrawal_request))
        }
        Err(e) => Err(WithdrawalError::from(e)),
    }
}
```

**File:** rs/ethereum/cketh/minter/src/tx.rs (L461-485)
```rust
    pub async fn sign(self) -> Result<SignedEip1559TransactionRequest, String> {
        let hash = self.hash();
        let key_name = read_state(|s| s.ecdsa_key_name.clone());
        let signature = crate::management::sign_with_ecdsa(
            key_name,
            DerivationPath::new(crate::MAIN_DERIVATION_PATH),
            hash.0,
        )
        .await
        .map_err(|e| format!("failed to sign tx: {e}"))?;
        let recid = compute_recovery_id(&hash, &signature).await;
        if recid.is_x_reduced() {
            return Err("BUG: affine x-coordinate of r is reduced which is so unlikely to happen that it's probably a bug".to_string());
        }
        let (r_bytes, s_bytes) = split_in_two(signature);
        let r = u256::from_be_bytes(r_bytes);
        let s = u256::from_be_bytes(s_bytes);
        let sig = Eip1559Signature {
            signature_y_parity: recid.is_y_odd(),
            r,
            s,
        };

        Ok(SignedEip1559TransactionRequest::new(self, sig))
    }
```
