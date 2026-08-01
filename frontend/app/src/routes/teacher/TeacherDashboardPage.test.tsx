import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { TeacherDashboardPage } from "@/routes/teacher/TeacherDashboardPage";
import type { Course } from "@/types/education";

vi.mock("@/api/hooks/education", () => ({
  useCourses: vi.fn(),
}));

const { useCourses } = await import("@/api/hooks/education");

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <TeacherDashboardPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const courses: Course[] = [
  {
    id: "c1", name: "Algebra", code: "M101", description: "", teacher_id: "t1",
    student_ids: ["s1", "s2"], student_count: 2, created_at: 1, updated_at: 1,
  },
];

function mockCourses(data: Course[] | null, state: "loading" | "error" | "ok" = "ok") {
  (useCourses as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
    data: state === "ok" ? data : undefined,
    isLoading: state === "loading",
    isError: state === "error",
  });
}

describe("TeacherDashboardPage", () => {
  beforeEach(() => vi.clearAllMocks());

  it("renders loading state", () => {
    mockCourses(null, "loading");
    renderPage();
    expect(screen.getByText("加载中...")).toBeInTheDocument();
  });

  it("renders error state", () => {
    mockCourses(null, "error");
    renderPage();
    expect(screen.getByText(/加载失败/)).toBeInTheDocument();
  });

  it("renders empty state when the teacher owns no courses", () => {
    mockCourses([]);
    renderPage();
    expect(screen.getByText("还没有课程")).toBeInTheDocument();
  });

  it("lists owned courses with links into the course detail", () => {
    mockCourses(courses);
    renderPage();
    expect(screen.getByText("Algebra")).toBeInTheDocument();
    const link = screen.getByText("Algebra").closest("a");
    expect(link).not.toBeNull();
    expect(link?.getAttribute("href")).toBe("/teacher/courses/c1");
  });
});
