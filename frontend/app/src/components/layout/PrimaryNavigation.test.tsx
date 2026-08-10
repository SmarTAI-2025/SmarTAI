import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it } from "vitest";
import { I18nProvider } from "@/i18n/I18nProvider";
import { PrimaryNavigation } from "./PrimaryNavigation";

describe("PrimaryNavigation demo mode", () => {
  beforeEach(() => window.localStorage.setItem("smartai_locale", "zh-CN"));

  it("keeps reviewers inside the showcase, live runner, and current task", () => {
    render(
      <I18nProvider>
        <MemoryRouter initialEntries={["/tasks/asg_demo/questions"]}>
          <PrimaryNavigation demoLivePath="/frontier/live?taskId=asg_demo" demoTaskPath="/tasks/asg_demo" />
        </MemoryRouter>
      </I18nProvider>,
    );

    expect(screen.getByRole("link", { name: "产品介绍" })).toHaveAttribute("href", "/frontier");
    expect(screen.getByRole("link", { name: "Live Demo" })).toHaveAttribute("href", "/frontier/live?taskId=asg_demo");
    expect(screen.getByRole("link", { name: "当前任务" })).toHaveAttribute("href", "/tasks/asg_demo");
    expect(screen.queryByRole("link", { name: "工作台" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "新建任务" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "历史任务" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "课程资料库" })).not.toBeInTheDocument();
  });

  it("highlights only Live Demo on the live runner route", () => {
    render(
      <I18nProvider>
        <MemoryRouter initialEntries={["/frontier/live?taskId=asg_demo"]}>
          <PrimaryNavigation demoLivePath="/frontier/live?taskId=asg_demo" demoTaskPath="/tasks/asg_demo" />
        </MemoryRouter>
      </I18nProvider>,
    );

    expect(screen.getByRole("link", { name: "产品介绍" })).not.toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "Live Demo" })).toHaveAttribute("aria-current", "page");
  });
});
