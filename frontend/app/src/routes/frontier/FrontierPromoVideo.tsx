import { ExternalLink, Languages, VolumeX } from "lucide-react";
import { useEffect, useRef, useState, type SyntheticEvent } from "react";
import type { Locale } from "@/i18n/messages";

type PromoVideoLocale = "zh-CN" | "en-US";

type PromoVideoAsset = {
  src: string;
  languageLabel: string;
  ariaLabel: string;
};

type FrontierPromoVideoProps = {
  locale: Locale;
  youtubeUrls?: Partial<Record<PromoVideoLocale, string | undefined>>;
};

const promoVideos: Record<PromoVideoLocale, PromoVideoAsset> = {
  "zh-CN": {
    src: "/frontier-media/SmarTAI-Promo-Final-ZH-v4-1.mp4",
    languageLabel: "中文旁白",
    ariaLabel: "SmarTAI 中文旁白宣传片",
  },
  "en-US": {
    src: "/frontier-media/SmarTAI-Promo-Final-EN-v4-1.mp4",
    languageLabel: "English narration",
    ariaLabel: "SmarTAI promotional film with English narration",
  },
};

export function FrontierPromoVideo({ locale, youtubeUrls }: FrontierPromoVideoProps) {
  const [activeLocale, setActiveLocale] = useState<PromoVideoLocale>(locale);
  const videoRef = useRef<HTMLVideoElement>(null);
  const activeVideo = promoVideos[activeLocale];
  const youtubeUrl = normalizeYouTubeUrl(youtubeUrls?.[activeLocale]);
  const zh = locale === "zh-CN";

  useEffect(() => setActiveLocale(locale), [locale]);

  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    video.defaultMuted = true;
    video.muted = true;
    if (navigator.userAgent.toLowerCase().includes("jsdom")) return;
    void video.play().catch(() => {
      // Native controls remain available if an OS-level policy rejects autoplay.
    });
  }, [activeLocale]);

  return (
    <section id="film" className="frontier-film-section" aria-labelledby="frontier-film-title">
      <div className="frontier-film-shell">
        <div className="frontier-film-heading">
          <div>
            <p className="frontier-eyebrow">{zh ? "SmarTAI 宣传片" : "SmarTAI film"}</p>
            <h2 id="frontier-film-title">{zh ? "一分钟，看见从作答到洞察。" : "From student work to insight in one minute."}</h2>
            <p>
              {zh
                ? "视频由本站直接播放，默认静音并自动循环；需要声音时，可使用播放器控件开启。"
                : "The film plays directly from this site, loops automatically, and stays muted until you choose otherwise."}
            </p>
          </div>
          <div className="frontier-film-language" role="group" aria-label={zh ? "选择宣传片旁白语言" : "Choose film narration language"}>
            <span><Languages aria-hidden="true" /> {zh ? "旁白" : "Narration"}</span>
            {(Object.keys(promoVideos) as PromoVideoLocale[]).map((videoLocale) => (
              <button
                key={videoLocale}
                type="button"
                aria-pressed={activeLocale === videoLocale}
                onClick={() => setActiveLocale(videoLocale)}
              >
                {promoVideos[videoLocale].languageLabel}
              </button>
            ))}
          </div>
        </div>

        <div className="frontier-film-player">
          <video
            key={activeVideo.src}
            ref={videoRef}
            aria-label={activeVideo.ariaLabel}
            autoPlay
            controls
            controlsList="nodownload"
            loop
            muted
            onCanPlay={startMutedPlayback}
            playsInline
            preload="metadata"
          >
            <source src={activeVideo.src} type="video/mp4" />
            {zh ? "当前浏览器不支持 MP4 视频播放。" : "Your browser does not support MP4 video playback."}
          </video>
          <span className="frontier-film-muted-note"><VolumeX aria-hidden="true" /> {zh ? "默认静音" : "Muted by default"}</span>
        </div>

        <div className="frontier-film-footer">
          <span>{zh ? "本站视频优先 · 中英双语字幕 · 1920 × 1080" : "Local playback first · bilingual captions · 1920 × 1080"}</span>
          {youtubeUrl ? (
            <a href={youtubeUrl} target="_blank" rel="noreferrer">
              {zh ? "在 YouTube 网页观看" : "Watch on YouTube"}
              <ExternalLink aria-hidden="true" />
            </a>
          ) : null}
        </div>
      </div>
    </section>
  );
}

function startMutedPlayback(event: SyntheticEvent<HTMLVideoElement>) {
  const video = event.currentTarget;
  video.defaultMuted = true;
  video.muted = true;
  void video.play().catch(() => {
    // Some browser or OS policies still require an explicit user gesture.
  });
}

function normalizeYouTubeUrl(value: string | undefined) {
  if (!value) return undefined;

  try {
    const url = new URL(value);
    const hostname = url.hostname.toLowerCase().replace(/^www\./, "");
    if (url.protocol === "https:" && (hostname === "youtube.com" || hostname.endsWith(".youtube.com") || hostname === "youtu.be")) {
      return url.toString();
    }
  } catch {
    // Invalid or non-YouTube values intentionally hide the optional external link.
  }

  return undefined;
}
