import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { TeacherCoursesPage } from "@/routes/teacher/TeacherCoursesPage";
import type { Course } from "@/types/education";

vi.mock("@/api/hooks/education", () => ({
  useCourses: vi.fn(),
  useCreateCourse: vi.fn(),
  useDeleteCourse: vi.fn(),
}));

const { useCourses, useCreateCourse, useDeleteCourse } = await import("@/api/hooks/education");

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <TeacherCoursesPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const courses: Course[] = [
  { id: "c1", name: "高等数学", code: "MATH101", description: "", teacher_id: "t1", student_ids: ["s1", "s2"], student_count: 2, created_at: 1, updated_at: 1 },
  { id: "c2", name: "线性代数", code: "", description: "", teacher_id: "t1", student_ids: [], student_count: 0, created_at: 2, updated_at: 2 },
];

function mockCourses(data: Course[] | null, state: "loading" | "error" | "ok" = "ok") {
  (useCourses as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
    data: state === "ok" ? data : undefined,
    isLoading: state === "loading",
    isError: state === "error",
  });
  (useCreateCourse as unknown as ReturnType<typeof vi.fn>).mockReturnValue({ mutate: vi.fn(), isPending: false });
  (useDeleteCourse as unknown as ReturnType<typeof vi.fn>).mockReturnValue({ mutate: vi.fn(), isPending: false });
}

describe("TeacherCoursesPage", () => {
  beforeEach(() => vi.clearAllMocks());

  it("renders loading state", () => {
    mockCourses(null, "loading");
    renderPage();
    expect(screen.getByText("加载中...")).toBeInTheDocument();
  });

  it("renders empty state", () => {
    mockCourses([]);
    renderPage();
    expect(screen.getByText("暂无课程")).toBeInTheDocument();
  });

  it("lists courses with student counts", async () => {
    mockCourses(courses);
    renderPage();
    expect(await screen.findByText("高等数学")).toBeInTheDocument();
    expect(screen.getByText("线性代数")).toBeInTheDocument();
    expect(screen.getByText("2")).toBeInTheDocument();
    expect(screen.getByText("—")).toBeInTheDocument(); // empty code shown as —
  });

  it("disables the create button until a name is entered", () => {
    mockCourses([]);
    renderPage();
    const button = screen.getByRole("button", { name: "新建课程" });
    expect(button).toBeDisabled();
  });

  it("creates a course via the form", async () => {
    mockCourses([]);
    const mutate = vi.fn();
    (useCreateCourse as unknown as ReturnType<typeof vi.fn>).mockReturnValue({ mutate, isPending: false });
    (useDeleteCourse as unknown as ReturnType<typeof vi.fn>).mockReturnValue({ mutate: vi.fn(), isPending: false });
    const user = userEvent.setup();
    renderPage();
    await user.type(screen.getByPlaceholderText("高等数学"), "概率论");
    await user.click(screen.getByRole("button", { name: "新建课程" }));
    await waitFor(() => expect(mutate).toHaveBeenCalled());
    expect(mutate).toHaveBeenCalledWith(expect.objectContaining({ name: "概率论" }), expect.any(Object));
  });
});
