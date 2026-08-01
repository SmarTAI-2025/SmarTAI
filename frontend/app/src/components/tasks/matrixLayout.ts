export const MATRIX_QUESTION_COLUMN_WIDTH = 60;
export const MATRIX_ACTION_COLUMN_WIDTH = 72;

const IDENTITY_COMPRESSION_START = 3;
const IDENTITY_COMPRESSION_END = 10;

interface MatrixIdentityLayoutInput {
  questionCount: number;
  studentIds: string[];
  studentNames: string[];
  studentIdLabel: string;
  studentNameLabel: string;
  reserveIdStatusIcon?: boolean;
}

/**
 * Gives question columns priority as a matrix grows while keeping both
 * identity columns wide enough for their longest visible value. When content
 * itself needs more room, the table grows and its own scroll container takes
 * over instead of clipping the identity text.
 */
export function getMatrixIdentityLayout({
  questionCount,
  studentIds,
  studentNames,
  studentIdLabel,
  studentNameLabel,
  reserveIdStatusIcon = false,
}: MatrixIdentityLayoutInput) {
  const compression = clamp(
    (questionCount - IDENTITY_COMPRESSION_START)
      / (IDENTITY_COMPRESSION_END - IDENTITY_COMPRESSION_START),
    0,
    1,
  );
  const preferredStudentIdWidth = interpolate(136, 108, compression);
  const preferredStudentNameWidth = interpolate(116, 88, compression);
  const studentIdContentWidth = widestText([studentIdLabel, ...studentIds])
    + 24
    + (reserveIdStatusIcon ? 16 : 0);
  const studentNameContentWidth = widestText([studentNameLabel, ...studentNames]) + 24;

  return {
    studentIdWidth: Math.ceil(Math.max(preferredStudentIdWidth, studentIdContentWidth)),
    studentNameWidth: Math.ceil(Math.max(preferredStudentNameWidth, studentNameContentWidth)),
  };
}

function widestText(values: string[]) {
  return Math.max(0, ...values.map(estimateTextWidth));
}

/** Conservative estimate for the 12px matrix type used by both workspaces. */
function estimateTextWidth(value: string) {
  return Array.from(value).reduce((width, character) => {
    if (/\p{Script=Han}|\p{Script=Hiragana}|\p{Script=Katakana}|\p{Script=Hangul}/u.test(character)) {
      return width + 12;
    }
    if (/[MW@#%&]/.test(character)) return width + 9;
    if (/[ilI1|.,'` ]/.test(character)) return width + 4;
    return width + 7.25;
  }, 0);
}

function interpolate(start: number, end: number, progress: number) {
  return start + (end - start) * progress;
}

function clamp(value: number, min: number, max: number) {
  return Math.min(max, Math.max(min, value));
}
