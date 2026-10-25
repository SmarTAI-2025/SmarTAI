# B1 raw JSON reliability baseline

Production baseline: `109acaec30db917b52c0ca827f15a4b5830647e8`.
Corpus SHA256: `c2a2655d7816a4e6a4b407646472980e4b9119e3e49174b87a5eb457fca84736`.

Synthetic program-path observations only; **not model accuracy**. No upstream calls or paid usage.

All-case denominator 42; scores 40; detailed 33; ledger 26; contract passes 17.
Checks-empty opportunities executed 0/6.

| Case | Status / score | Modes | Tools | Review entries | Contract failures |
|---|---|---|---:|---:|---|
| R01_real_tool_feedback | scored / 0.0 | points | 1 | 1 | none |
| R02_raw_tex_claim_context | scored / 4.0 | points | 0 | 0 | score_expected_10_observed_4.0; detailed_structure_lost; expected_tool_evidence_missing; source_text_not_preserved |
| R03_numeric_checks_empty | scored / 10.0 | points | 0 | 0 | tools_required_but_not_executed |
| R04_symbolic_checks_empty | scored / 10.0 | points | 0 | 0 | tools_required_but_not_executed |
| R05_physics_units_empty | scored / 10.0 | points | 0 | 0 | tools_required_but_not_executed |
| R06_pi_constant | scored / 10.0 | points | 0 | 0 | tools_required_but_not_executed |
| R07_e_constant | scored / 10.0 | points | 0 | 0 | tools_required_but_not_executed |
| R08_i_constant | scored / 10.0 | points | 0 | 0 | tools_required_but_not_executed |
| R09_alternative_proof | scored / 10.0 | points | 0 | 0 | none |
| R10_correct_result_wrong_work | scored / 2.0 | points | 0 | 0 | none |
| R11_correct_work_wrong_final | scored / 8.0 | points | 0 | 0 | none |
| R12_award_claim_mapping | scored / 10.0 | points | 0 | 0 | award_uses_claim_of_different_item |
| R13_follow_through | scored / 8.0 | deductions | 0 | 0 | score_expected_9_observed_8.0 |
| R14_independent_second_error | scored / 8.0 | deductions | 0 | 0 | none |
| R15_chinese_provided | scored / 4.0 | holistic | 0 | 0 | score_expected_9_observed_4.0; required_ledger_mode_lost |
| R16_chinese_ai_tamper | scored / 1.0 | deductions | 0 | 0 | teacher_amount_overridden_by_ai_label |
| R17_arabic_ai_tamper | scored / 4.0 | holistic | 0 | 0 | none |
| R18_question_total | scored / 4.0 | holistic | 0 | 0 | score_expected_9_observed_4.0; required_ledger_mode_lost |
| R19_english_count_cap | scored / 4.0 | holistic | 0 | 0 | score_expected_9_observed_4.0; required_ledger_mode_lost |
| R20_minor_exemption | scored / 4.0 | holistic | 0 | 0 | score_expected_10_observed_4.0; required_ledger_mode_lost |
| R21_mixed_direct_review | scored / 5.5 | points | 0 | 0 | review_required_but_not_executed |
| R22_refuted_feedback_parse_failure | scored / 10.0 | points | 1 | 1 | unresolved_tool_conflict_has_no_specific_nonblocking_advice |
| R23_shared_review_budget | scored / 9.0 | points | 2 | 3 | review_budget_exceeded |
| R24_invented_domain | scored / 10.0 | points | 1 | 1 | ungrounded_tool_request_trusted |
| R25_invented_tolerance | scored / 10.0 | points | 1 | 1 | ungrounded_tool_request_trusted |
| R26_unrelated_tool_expression | scored / 10.0 | points | 1 | 1 | ungrounded_tool_request_trusted |
| R27_missing_obligation | scored / 5.0 | points | 0 | 0 | missing_obligation_not_retained |
| R28_explicit_contradiction | scored / 6.0 | points | 0 | 0 | none |
| R29_unknown_tool | scored / 10.0 | points | 1 | 0 | none |
| R30_provider_failure | failed / None | points | 0 | 0 | none |
| R31_cancelled | cancelled / None | points | 0 | 0 | none |
| R32_changed_exponent | scored / 4.0 | points | 0 | 0 | none |
| R33_changed_quantifier | scored / 4.0 | points | 0 | 0 | none |
| R34_invented_condition | scored / 4.0 | points | 0 | 0 | none |
| R35_compressed_chain | scored / 4.0 | points | 0 | 0 | none |
| R36_digit_boundary | scored / 4.0 | points | 0 | 0 | none |
| R37_local_quote_repair | scored / 4.0 | points | 0 | 0 | score_expected_10_observed_4.0; detailed_structure_lost; bounded_source_repair_not_executed |
| R38_holistic_without_claims | scored / 10.0 | holistic | 0 | 0 | empty_claims_accepted_as_detailed |
| R39_physics_energy_proof | scored / 10.0 | points | 0 | 0 | none |
| R40_math_concept | scored / 10.0 | points | 0 | 0 | none |
| R41_plan_display_source_separation | scored / 4.0 | holistic | 0 | 0 | score_expected_10_observed_4.0; required_ledger_mode_lost |
| R42_abstract_not_applicable | scored / 10.0 | points | 0 | 0 | explicit_not_applicable_decision_missing |

Boundary/version/recovery probes: 5/26 pass.
Missing matcher and unchanged-version failures are integration gates, not proof that every negative source is currently accepted.

| Probe | Pass | Evidence |
|---|---|---|
| J01_inline_source | False | source_fields_must_bypass_display_formatter |
| J02_display_source | False | source_fields_must_bypass_display_formatter |
| J03_source_whitespace | True | source_fields_must_bypass_display_formatter |
| S01_exact | False | source_matcher_missing |
| S02_inline_math | False | source_matcher_missing |
| S03_display_math | False | source_matcher_missing |
| S04_math_whitespace | False | source_matcher_missing |
| S05_exponent | False | source_matcher_missing |
| S06_quantifier | False | source_matcher_missing |
| S07_condition | False | source_matcher_missing |
| S08_chain_compression | False | source_matcher_missing |
| S09_digit_boundary | False | source_matcher_missing |
| S10_word_boundary | False | source_matcher_missing |
| S11_sign | False | source_matcher_missing |
| S12_ambiguous | False | source_matcher_missing |
| S13_context_resolves | False | source_matcher_missing |
| S14_unpaired_delimiter | False | source_matcher_missing |
| V01_engine | False | old_version_must_be_retired |
| V02_schema | False | old_version_must_be_retired |
| V03_interpretation | False | old_version_must_be_retired |
| V04_legacy_snapshot | False | old_snapshot_must_not_restore |
| V05_legacy_trace | False | old_expert_trace_must_not_enter_new_engine |
| L01_cancel_context | True | cancelled_batch_context_reset |
| L02_same_question_retry | True | new_batch_recompiles_after_cancel |
| L03_wrong_engine_snapshot | True | incompatible_engine_rejected |
| L04_wrong_engine_expert | True | incompatible_expert_rejected |
