import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { FrontierPromoVideo } from "./FrontierPromoVideo";

describe("FrontierPromoVideo", () => {
  it("autoplays one local language edition muted and loops it", () => {
    const { container } = render(<FrontierPromoVideo locale="en-US" />);
    const video = screen.getByLabelText("SmarTAI promotional film with English narration") as HTMLVideoElement;

    expect(video.autoplay).toBe(true);
    expect(video.controls).toBe(true);
    expect(video.loop).toBe(true);
    expect(video.muted).toBe(true);
    expect(video.playsInline).toBe(true);
    expect(screen.getByRole("heading", { name: "From student work to insight in one minute." })).toBeInTheDocument();
    expect(screen.queryByText(/Local playback first/)).not.toBeInTheDocument();
    expect(container.querySelectorAll("video source")).toHaveLength(1);
    expect(container.querySelector("video source")).toHaveAttribute(
      "src",
      "/frontier-media/SmarTAI-Promo-Final-EN-v4-1.mp4",
    );
  });

  it("uses the one-minute Chinese headline", () => {
    render(<FrontierPromoVideo locale="zh-CN" />);

    expect(screen.getByRole("heading", { name: "一分钟，看见从作答到洞察。" })).toBeInTheDocument();
    expect(screen.queryByText(/66 秒/)).not.toBeInTheDocument();
    expect(screen.queryByText(/本站视频优先/)).not.toBeInTheDocument();
  });

  it("switches editions without loading both videos and exposes a configured YouTube link", () => {
    const { container } = render(
      <FrontierPromoVideo
        locale="en-US"
        youtubeUrls={{
          "zh-CN": "https://www.youtube.com/watch?v=zh-video",
          "en-US": "https://youtu.be/en-video",
        }}
      />,
    );

    expect(screen.getByRole("link", { name: /watch on youtube/i })).toHaveAttribute("href", "https://youtu.be/en-video");
    fireEvent.click(screen.getByRole("button", { name: "中文旁白" }));

    expect(screen.getByLabelText("SmarTAI 中文旁白宣传片")).toBeInTheDocument();
    expect(container.querySelectorAll("video source")).toHaveLength(1);
    expect(container.querySelector("video source")).toHaveAttribute(
      "src",
      "/frontier-media/SmarTAI-Promo-Final-ZH-v4-1.mp4",
    );
    expect(screen.getByRole("link", { name: /watch on youtube/i })).toHaveAttribute(
      "href",
      "https://www.youtube.com/watch?v=zh-video",
    );
  });

  it("hides invalid or unconfigured external links", () => {
    render(
      <FrontierPromoVideo
        locale="en-US"
        youtubeUrls={{ "en-US": "https://example.com/not-youtube" }}
      />,
    );

    expect(screen.queryByRole("link", { name: /watch on youtube/i })).not.toBeInTheDocument();
  });
});
