/**
 * Hidden/future-only contracts for possible LMS/LTI integration.
 *
 * Product constraint: these types must not be surfaced in routes, navigation,
 * empty states, or current teacher workflows. They exist only to keep future
 * API experiments typed without implying LMS support is available.
 */
export interface HiddenFutureExternalCourseMapping {
  external_course_id: string;
  provider: "canvas" | "moodle" | "lti" | string;
  smartai_course_id: string;
}

export interface HiddenFutureExternalAssignmentMapping {
  external_assignment_id: string;
  provider: "canvas" | "moodle" | "lti" | string;
  smartai_assignment_id: string;
}
