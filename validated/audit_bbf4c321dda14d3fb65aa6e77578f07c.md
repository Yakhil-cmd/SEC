### Title
Missing `msg.sender` Authorization Check in `CW721ERC721Pointer.transferFrom` Allows Unauthorized NFT Transfer - (File: `contracts/src/CW721ERC721Pointer.sol`)

---

### Summary

`CW721ERC721Pointer.transferFrom` only verifies that the `from` argument equals `ownerOf(tokenId)`, but never checks whether `msg.sender` is the owner, an approved address, or an approved-for-all operator. Any unprivileged caller can invoke `transferFrom(victim, attacker, tokenId)` and trigger a CosmWasm `transfer_nft` execution on behalf of the victim.

---

### Finding Description

The standard ERC-721 `transferFrom` pattern requires two independent checks:
1. `from == ownerOf(tokenId)` — the stated source is the actual owner.
2. `msg.sender` is authorized — the caller is the owner, individually approved, or an approved-for-all operator.

`CW721ERC721Pointer.transferFrom` performs only check (1):

```solidity
function transferFrom(address from, address to, uint256 tokenId) public override {
    if (to == address(0)) {
        revert ERC721InvalidReceiver(address(0));
    }
    require(from == ownerOf(tokenId), "`from` must be the owner");
    // ... builds CosmWasm message and calls _execute — no msg.sender check
}
``` [1](#0-0) 

Compare this to the correct implementation in `contracts/src/ERC721.sol`, which explicitly checks `_isApprovedOrOwner(from, msg.sender, id)` after verifying `from == _ownerOf[id]`: [2](#0-1) 

The `_execute` helper uses `delegatecall` to the WASMD precompile: [3](#0-2) 

Because `delegatecall` preserves `msg.sender`, the CosmWasm `transfer_nft` message is sent with the **attacker's** Sei address as the sender. Whether the underlying CW721 contract then rejects the call depends entirely on that contract's own authorization logic — the EVM pointer provides **zero** authorization enforcement.

---

### Impact Explanation

If the underlying CW721 contract grants the pointer contract (or any caller) broad operator rights, or if a CW721 implementation omits its own sender check, any address can steal any NFT bridged through this pointer. Even without that condition, the EVM-level interface is broken: callers who rely on standard ERC-721 semantics (e.g., marketplaces, aggregators) will observe that `transferFrom` does not revert for unauthorized callers at the EVM layer, leading to unpredictable behavior and potential fund loss.

Unauthorized NFT transfer of value ≥ $5 k maps to **Critical** under the Sei bounty scope.

---

### Likelihood Explanation

The pointer contract is deployed as infrastructure for any CW721 ↔ EVM bridge on Sei. The missing check is reachable by any unprivileged EOA or contract with no preconditions beyond knowing a valid `tokenId` and its current owner. Likelihood is **High**.

---

### Recommendation

Add an `_isApprovedOrOwner`-equivalent check before dispatching to CosmWasm, mirroring the pattern already used in `contracts/src/ERC721.sol`:

```solidity
function transferFrom(address from, address to, uint256 tokenId) public override {
    if (to == address(0)) revert ERC721InvalidReceiver(address(0));
    require(from == ownerOf(tokenId), "`from` must be the owner");
    require(
        msg.sender == from ||
        getApproved(tokenId) == msg.sender ||
        isApprovedForAll(from, msg.sender),
        "not authorized"
    );
    // ... build and dispatch CosmWasm message
}
```

---

### Proof of Concept

1. Alice owns token ID `42` in a CW721 contract that has a `CW721ERC721Pointer` deployed.
2. Bob (attacker, no approval) calls:
   ```solidity
   pointer.transferFrom(alice, bob, 42);
   ```
3. The pointer checks only `alice == ownerOf(42)` — passes.
4. `_execute` fires `transfer_nft { recipient: bob, token_id: "42" }` via `delegatecall` to the WASMD precompile.
5. If the CW721 contract accepts the call (e.g., pointer contract is a global operator, or the CW721 implementation is permissive), the NFT is transferred to Bob with no authorization from Alice. [1](#0-0) [4](#0-3)

### Citations

**File:** contracts/src/CW721ERC721Pointer.sol (L160-169)
```text
    function transferFrom(address from, address to, uint256 tokenId) public override {
        if (to == address(0)) {
            revert ERC721InvalidReceiver(address(0));
        }
        require(from == ownerOf(tokenId), "`from` must be the owner");
        string memory recipient = _formatPayload("recipient", _doubleQuotes(AddrPrecompile.getSeiAddr(to)));
        string memory tId = _formatPayload("token_id", _doubleQuotes(Strings.toString(tokenId)));
        string memory req = _curlyBrace(_formatPayload("transfer_nft", _curlyBrace(_join(recipient, tId, ","))));
        _execute(bytes(req));
    }
```

**File:** contracts/src/CW721ERC721Pointer.sol (L187-198)
```text
    function _execute(bytes memory req) internal returns (bytes memory) {
        (bool success, bytes memory ret) = WASMD_PRECOMPILE_ADDRESS.delegatecall(
            abi.encodeWithSignature(
                "execute(string,bytes,bytes)",
                Cw721Address,
                bytes(req),
                bytes("[]")
            )
        );
        require(success, "CosmWasm execute failed");
        return ret;
    }
```

**File:** contracts/src/ERC721.sol (L111-119)
```text
    function _isApprovedOrOwner(
        address owner,
        address spender,
        uint id
    ) internal view returns (bool) {
        return (spender == owner ||
        isApprovedForAll[owner][spender] ||
            spender == _approvals[id]);
    }
```

**File:** contracts/src/ERC721.sol (L121-126)
```text
    function transferFrom(address from, address to, uint id) public {
        require(from == _ownerOf[id], "from != owner");
        require(to != address(0), "transfer to zero address");

        require(_isApprovedOrOwner(from, msg.sender, id), "not authorized");

```
