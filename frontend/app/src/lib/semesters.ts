import type { MessageKey } from "@/i18n/messages";

export interface SemesterOption {
  id: string;
  academicYear: string;
  season: "autumn" | "winter" | "spring" | "summer";
}

export function buildSemesterOptions(now = new Date()): SemesterOption[] {
  const end = nextSemester(getCurrentSemester(now));
  const options: SemesterOption[] = [];
  let cursor = semester(2025, "autumn");
  while (semesterRank(cursor) <= semesterRank(end) && options.length < 40) {
    options.push(cursor);
    cursor = nextSemester(cursor);
  }
  return options;
}

export function getCurrentSemesterId(now = new Date()): string {
  return getCurrentSemester(now).id;
}

export function formatSemesterLabel(id: string, t: (key: MessageKey) => string): string {
  const match = /^(\d{4}-\d{4})-(autumn|winter|spring|summer)$/.exec(id);
  if (!match) return id;
  const seasonKeys: Record<string, MessageKey> = {
    autumn: "historySemesterAutumn",
    winter: "historySemesterWinter",
    spring: "historySemesterSpring",
    summer: "historySemesterSummer",
  };
  return `${match[1]} ${t(seasonKeys[match[2]])}`;
}

function getCurrentSemester(now: Date): SemesterOption {
  const year = now.getFullYear();
  const month = now.getMonth() + 1;
  if (month >= 8 && month <= 11) return semester(year, "autumn");
  if (month === 12) return semester(year, "winter");
  if (month <= 2) return semester(year - 1, "winter");
  if (month <= 5) return semester(year - 1, "spring");
  return semester(year - 1, "summer");
}

function semester(startYear: number, season: SemesterOption["season"]): SemesterOption {
  const academicYear = `${startYear}-${startYear + 1}`;
  return { id: `${academicYear}-${season}`, academicYear, season };
}

function nextSemester(current: SemesterOption): SemesterOption {
  const startYear = Number(current.academicYear.slice(0, 4));
  if (current.season === "autumn") return semester(startYear, "winter");
  if (current.season === "winter") return semester(startYear, "spring");
  if (current.season === "spring") return semester(startYear, "summer");
  return semester(startYear + 1, "autumn");
}

function semesterRank(value: SemesterOption): number {
  const startYear = Number(value.academicYear.slice(0, 4));
  return startYear * 4 + ["autumn", "winter", "spring", "summer"].indexOf(value.season);
}
