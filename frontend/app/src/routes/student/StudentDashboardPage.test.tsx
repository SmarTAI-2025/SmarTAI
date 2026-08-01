import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StudentDashboardPage } from "@/routes/student/StudentDashboardPage";
import type { Course } from "@/types/education";
import type { User } from "@/types/auth";

vi.mock("@/api/hooks/education", () => ({
  useCourses: vi.fn(),
}));
vi.mock("@/api/hooks", () => ({
  useCurrentUser: vi.fn(),
}));

const { useCourses } = await import("@/api/hooks/education");
const { useCurrentUser } = await import("@/api/hooks");

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <StudentDashboardPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const student: User = { id: "s1", username: "stu", email: "", role: "student", is_active: true, created_at: 1 };
const courses: Course[] = [
  { id: "c1", name: "Algebra", code: "M", description: "", teacher_id: "t1",
    student_ids: ["s1"], student_count: 1, created_at: 1, updated_at: 1 },
];

describe("StudentDashboardPage", () => {
  beforeEach(() => vi.clearAllMocks());

  it("renders loading state", () => {
    (useCurrentUser as unknown as ReturnType<typeof vi.fn>).mockReturnValue({ data: student, isLoading: false });
    (useCourses as unknown as ReturnType<typeof vi.fn>).mockReturnValue({ data: undefined, isLoading: true });
    renderPage();
    expect(screen.getByText("加载中...")).toBeInTheDocument();
  });

  it("renders empty state when the student is enrolled in nothing", () => {
    (useCurrentUser as unknown as ReturnType<typeof vi.fn>).mockReturnValue({ data: student, isLoading: false });
    (useCourses as unknown as ReturnType<typeof vi.fn>).mockReturnValue({ data: [], isLoading: false });
    renderPage();
    expect(screen.getByText(/还没有课程|暂无课程|没有课程/)).toBeInTheDocument();
  });

  it("lists enrolled courses", () => {
    (useCurrentUser as unknown as ReturnType<typeof vi.fn>).mockReturnValue({ data: student, isLoading: false });
    (useCourses as unknown as ReturnType<typeof vi.fn>).mockReturnValue({ data: courses, isLoading: false });
    renderPage();
    expect(screen.getByText("Algebra")).toBeInTheDocument();
  });
});
