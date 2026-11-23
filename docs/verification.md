# Verification and report traceability

## Baseline

Software version: **5.0.0**. Source package: **HERC_2027_Pi5_Task_Code_Rev5.zip**. Design contribution: **HERC_2027_RC_Autonomous_Task_DR_Submission_Rev6.docx**, Appendix A. The Rev6 document split did not change implementation files.

All **33** source/configuration listings were compared with the latest report and match exactly. [report-source-manifest.json](report-source-manifest.json) records the SHA-256 digest of each corresponding file. Repository documentation, the host dependency specification and the test workflow are additions to that baseline.

## Host regression tests

The existing release record reports **93 passing tests, no failures and no skips**. The suite covers packet parsing, invalid and out-of-range commands, receiver arming, stale inputs, Pi output state, substituted GPIO/I2C, camera metadata, calibrated geometry, image filtering, evidence storage, duplicate attempts and local viewing.

The tests use synthetic images and substituted devices. They do not establish physical rover performance. GitHub Actions repeats the host suite on Python 3.11 and 3.12; the Actions tab is the record of each CI result.

The repository copy was checked again on 23 November 2026: **93 tests passed, with no failures or skips**, using Windows, Python 3.12.14, NumPy 2.5.5, OpenCV 5.1.0 and Pillow 13.0.0. The first attempt lacked OpenCV in the selected environment; the complete run followed installation of the documented host dependencies. No implementation change was needed.

## Physical verification still open

- Actual Pi OS/kernel/camera stack and shared-computer workload compatibility.
- Supply voltage/current, heat, receiver input protection and OE behavior during faults.
- Pan-angle accuracy, repeatability, travel clearance and settling time.
- Measured camera calibration and outdoor target ranging against independent distances.
- Actual radio reception and rejection under rover electrical noise.
- Offline timestamps, saved-result display and full task rehearsals.
- Organizer confirmation of radio parameters, sequence interpretation, digital filters, ranging reference and permitted initiation/retry behavior.

No physical test results, organizer approvals or measured calibration values are fabricated. The templates remain incomplete until the team supplies the corresponding evidence.

## Documentation split

The DR contribution contains the design, research, calculations, implementation and verification status. Installation and operating procedures are separate; their software-specific content is available in [setup-and-operation.md](setup-and-operation.md). The Word reports and original NASA reference documents are not duplicated in this source repository.

## CI references

The workflow uses the documented GitHub-maintained [checkout](https://github.com/actions/checkout) and [setup-python](https://github.com/actions/setup-python) actions with read-only repository contents permission.
