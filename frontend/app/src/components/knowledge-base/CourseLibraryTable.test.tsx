import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { CourseLibraryTable } from "@/components/knowledge-base/CourseLibraryTable";
import type { CourseMaterial } from "@/types";

const material: CourseMaterial = {
  material_id: "material-1",
  course_id: null,
  group_id: null,
  filename: "SmarTAI_hw2.txt",
  category: "rubric",
  labels: ["评分标准"],
  content_type: "text/plain",
  size_bytes: 1024,
  sha256: "abc",
  created_at: 1,
  updated_at: 1,
  last_used_at: null,
  task_reference_count: 0,
  parse_status: "ready",
  group_name: null,
  course_name: null,
  course_code: null,
  match_kind: null,
  match_score: null,
  match_reason: null,
};

describe("CourseLibraryTable", () => {
  it("shows separate edit and delete actions for every material", async () => {
    const onEditMaterial = vi.fn();
    const onDeleteMaterial = vi.fn();
    const user = userEvent.setup();

    render(
      <CourseLibraryTable
        materials={[material]}
        groups={[]}
        locale="zh-CN"
        isLoading={false}
        hasError={false}
        query=""
        showGroups
        onOpenGroup={vi.fn()}
        onEditGroup={vi.fn()}
        onEditMaterial={onEditMaterial}
        onDeleteMaterial={onDeleteMaterial}
        onRetry={vi.fn()}
      />,
    );

    await user.click(screen.getByRole("button", { name: "编辑资料 SmarTAI_hw2.txt" }));
    await user.click(screen.getByRole("button", { name: "删除资料 SmarTAI_hw2.txt" }));

    expect(onEditMaterial).toHaveBeenCalledWith(material);
    expect(onDeleteMaterial).toHaveBeenCalledWith(material);
    expect(screen.queryByRole("button", { name: "管理资料 SmarTAI_hw2.txt" })).not.toBeInTheDocument();
  });
});
