# B2 raw JSON independent acceptance

Production baseline: `109acaec30db917b52c0ca827f15a4b5830647e8`; candidate: `d51a8474c275115abb46ac2961f0954d0470ed2f`.
Corpus SHA256: `c2a2655d7816a4e6a4b407646472980e4b9119e3e49174b87a5eb457fca84736`.

Synthetic program-path observations only; **not model accuracy**. No upstream calls or paid usage.

All-case denominator 42; scores 40; detailed 33; ledger 29; contract passes 39.
Checks-empty opportunities executed 6/6.

| Case | Status / score | Modes | Tools | Review entries | Contract failures |
|---|---|---|---:|---:|---|
| R01_real_tool_feedback | scored / 0.0 | points | 2 | 1 | none |
| R02_raw_tex_claim_context | scored / 10.0 | points | 2 | 1 | none |
| R03_numeric_checks_empty | scored / 10.0 | points | 1 | 1 | none |
| R04_symbolic_checks_empty | scored / 10.0 | points | 1 | 1 | none |
| R05_physics_units_empty | scored / 10.0 | points | 1 | 1 | none |
| R06_pi_constant | scored / 10.0 | points | 1 | 1 | none |
| R07_e_constant | scored / 10.0 | points | 1 | 1 | none |
| R08_i_constant | scored / 10.0 | points | 1 | 1 | none |
| R09_alternative_proof | scored / 10.0 | points | 0 | 0 | none |
| R10_correct_result_wrong_work | scored / 2.0 | points | 0 | 0 | none |
| R11_correct_work_wrong_final | scored / 8.0 | points | 0 | 0 | none |
| R12_award_claim_mapping | scored / 4.0 | points | 0 | 0 | none |
| R13_follow_through | scored / 4.0 | holistic | 2 | 1 | score_expected_9_observed_4.0; required_ledger_mode_lost |
| R14_independent_second_error | scored / 4.0 | holistic | 2 | 1 | score_expected_8_observed_4.0; required_ledger_mode_lost |
| R15_chinese_provided | scored / 9.0 | deductions | 1 | 1 | none |
| R16_chinese_ai_tamper | scored / 4.0 | holistic | 1 | 1 | none |
| R17_arabic_ai_tamper | scored / 4.0 | holistic | 1 | 1 | none |
| R18_question_total | scored / 9.0 | deductions | 1 | 1 | none |
| R19_english_count_cap | scored / 9.0 | deductions | 1 | 1 | none |
| R20_minor_exemption | scored / 10.0 | deductions | 0 | 0 | none |
| R21_mixed_direct_review | scored / 5.5 | points | 1 | 1 | none |
| R22_refuted_feedback_parse_failure | scored / 10.0 | points | 2 | 1 | none |
| R23_shared_review_budget | scored / 9.0 | points | 4 | 1 | none |
| R24_invented_domain | scored / 10.0 | points | 1 | 1 | none |
| R25_invented_tolerance | scored / 10.0 | points | 1 | 1 | none |
| R26_unrelated_tool_expression | scored / 10.0 | points | 1 | 1 | none |
| R27_missing_obligation | scored / 5.0 | points | 0 | 0 | none |
| R28_explicit_contradiction | scored / 6.0 | points | 0 | 0 | none |
| R29_unknown_tool | scored / 10.0 | points | 0 | 0 | expected_tool_evidence_missing |
| R30_provider_failure | failed / None | points | 0 | 0 | none |
| R31_cancelled | cancelled / None | points | 0 | 0 | none |
| R32_changed_exponent | scored / 4.0 | points | 0 | 0 | none |
| R33_changed_quantifier | scored / 4.0 | points | 0 | 0 | none |
| R34_invented_condition | scored / 4.0 | points | 0 | 0 | none |
| R35_compressed_chain | scored / 4.0 | points | 0 | 0 | none |
| R36_digit_boundary | scored / 4.0 | points | 0 | 0 | none |
| R37_local_quote_repair | scored / 10.0 | points | 2 | 1 | none |
| R38_holistic_without_claims | scored / 4.0 | holistic | 0 | 0 | none |
| R39_physics_energy_proof | scored / 10.0 | points | 0 | 0 | none |
| R40_math_concept | scored / 10.0 | points | 0 | 0 | none |
| R41_plan_display_source_separation | scored / 10.0 | points | 1 | 1 | none |
| R42_abstract_not_applicable | scored / 10.0 | points | 0 | 0 | none |

Boundary/version/recovery probes: 26/26 pass.
Missing matcher and unchanged-version failures are integration gates, not proof that every negative source is currently accepted.

| Probe | Pass | Evidence |
|---|---|---|
| J01_inline_source | True | source_fields_must_bypass_display_formatter |
| J02_display_source | True | source_fields_must_bypass_display_formatter |
| J03_source_whitespace | True | source_fields_must_bypass_display_formatter |
| S01_exact | True | source_location_contract |
| S02_inline_math | True | source_location_contract |
| S03_display_math | True | source_location_contract |
| S04_math_whitespace | True | source_location_contract |
| S05_exponent | True | source_location_contract |
| S06_quantifier | True | source_location_contract |
| S07_condition | True | source_location_contract |
| S08_chain_compression | True | source_location_contract |
| S09_digit_boundary | True | source_location_contract |
| S10_word_boundary | True | source_location_contract |
| S11_sign | True | source_location_contract |
| S12_ambiguous | True | source_location_contract |
| S13_context_resolves | True | source_location_contract |
| S14_unpaired_delimiter | True | source_location_contract |
| V01_engine | True | old_version_must_be_retired |
| V02_schema | True | old_version_must_be_retired |
| V03_interpretation | True | old_version_must_be_retired |
| V04_legacy_snapshot | True | old_snapshot_must_not_restore |
| V05_legacy_trace | True | old_expert_trace_must_not_enter_new_engine |
| L01_cancel_context | True | cancelled_batch_context_reset |
| L02_same_question_retry | True | new_batch_recompiles_after_cancel |
| L03_wrong_engine_snapshot | True | incompatible_engine_rejected |
| L04_wrong_engine_expert | True | incompatible_expert_rejected |
