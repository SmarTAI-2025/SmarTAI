# B2 fixed-candidate safety acceptance

Revision: `d51a8474c275115abb46ac2961f0954d0470ed2f`; production baseline `109acaec30db917b52c0ca827f15a4b5830647e8`.

Disposable SQLite and synthetic provider only; the capability gate is reported separately.

All-case denominator 18; outcomes: `{'passed': 18}`; exit 0.

| Probe | Outcome | Evidence |
|---|---|---|
| test_durable_stop_two_retries_reuse_exact_plans_and_valid_zero_without_provider | passed | {"synthetic_provider_entries": 4, "retry_provider_entries": 0} |
| test_changed_inputs_cannot_reuse_failed_only_checkpoint[criterion] | passed | {} |
| test_changed_inputs_cannot_reuse_failed_only_checkpoint[setup] | passed | {} |
| test_persisted_obsolete_engine_never_reuses_old_math_score_or_plan | passed | {} |
| test_stopped_worker_cannot_append_checkpoints | passed | {} |
| test_storage_fault_propagates_without_publishing_a_fallback[plan] | passed | {"fault_boundary": "plan", "published_results": 0} |
| test_storage_fault_propagates_without_publishing_a_fallback[expert] | passed | {"fault_boundary": "expert", "published_results": 0} |
| test_storage_fault_propagates_without_publishing_a_fallback[final] | passed | {"fault_boundary": "final", "published_results": 0} |
| test_reporter_failure_is_not_a_holistic_plan[OSError-before_plan] | passed | {"fault_boundary": "before_plan", "failure_type": "OSError", "propagated_type": "CheckpointWriteError", "cause_type": "OSError"} |
| test_reporter_failure_is_not_a_holistic_plan[OSError-after_plan] | passed | {"fault_boundary": "after_plan", "failure_type": "OSError", "propagated_type": "CheckpointWriteError", "cause_type": "OSError"} |
| test_reporter_failure_is_not_a_holistic_plan[OSError-before_substep] | passed | {"fault_boundary": "before_substep", "failure_type": "OSError", "propagated_type": "CheckpointWriteError", "cause_type": "OSError"} |
| test_reporter_failure_is_not_a_holistic_plan[CheckpointWriteError-before_plan] | passed | {"fault_boundary": "before_plan", "failure_type": "CheckpointWriteError"} |
| test_reporter_failure_is_not_a_holistic_plan[CheckpointWriteError-after_plan] | passed | {"fault_boundary": "after_plan", "failure_type": "CheckpointWriteError"} |
| test_reporter_failure_is_not_a_holistic_plan[CheckpointWriteError-before_substep] | passed | {"fault_boundary": "before_substep", "failure_type": "CheckpointWriteError"} |
| test_assessment_reporter_failure_propagates_through_batch[OSError] | passed | {"failure_type": "OSError", "propagated_type": "CheckpointWriteError", "cause_type": "OSError"} |
| test_assessment_reporter_failure_propagates_through_batch[CheckpointWriteError] | passed | {"failure_type": "CheckpointWriteError"} |
| test_actual_math_worker_cancellation_reaps_process_and_releases_capacity | passed | {"worker_returncode": -9, "worker_kind": "real production math subprocess"} |
| test_public_activity_pagination_and_progress_omit_private_evidence | passed | {"private_events": 4, "public_history_pages": 35} |
