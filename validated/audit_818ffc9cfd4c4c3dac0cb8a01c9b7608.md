After thorough analysis of the Aurora Engine codebase, I identified the following analog vulnerability:

---

### Title
Malicious NEP-141 Token Contract Can Directly Call `ft_on_transfer` to Mint Unbacked ERC-20 Mirror Tokens — (File: `engine/src/contract_methods/connector.rs`)

### Summary
The `ft_on_transfer` function mints ERC-20 mirror tokens based solely on the caller-supplied `amount` field in JSON arguments, without verifying that actual NEP-141 tokens were transferred to the engine beforehand. Any account registered as a NEP-141 token in the engine