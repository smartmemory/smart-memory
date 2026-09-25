# Live lesson lifecycle replay

The two response files preserve the JSON payloads from the controller's
p4-live-classifier-responses.txt (S1 and S2). S0 has no classifier call.
The original full transcript and extraction responses were not supplied;
sessions.json reconstructs the three sessions from the brief and verbatim
quoted spans in the saved responses. Its lesson texts are replay inputs,
not claimed to be saved extraction output.

The test remaps recorded decision IDs to fresh store IDs and removes only
S2 rows outside the current candidate set (the old platform rule is now
retired). All classifier judgments, evidence, reasons and flags are preserved.
The saved S1 response has no primary field, so lesson index 0 is the documented
fallback successor; the security-team lesson at index 1 is also active.
