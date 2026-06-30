Looking at the Aurora Engine codebase, I need to find an analog to the "permanent fund freeze on state transition" pattern from the Voter.sol report. The vulnerability class is: **funds committed during one execution phase become permanently locked when a subsequent asynchronous phase fails, with no refund path**.

Let me examine the XCC precompile and its promise handling.