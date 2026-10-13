import { useQueryClient } from "@tanstack/react-query";
import { useRef, type ComponentProps, type MouseEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { normalizeAPIError, postJSON, type AuthAwareRequestConfig } from "@/api/client";
import { useDraftLeave } from "@/hooks/useDraftLeave";
import { useI18n } from "@/i18n/I18nProvider";
import { endFrontierSessionLocally, getFrontierToken, isFrontierExitPending, setFrontierExitPending } from "@/lib/frontierSession";
import { useFrontierWorkspace } from "@/lib/frontierContext";

export function FrontierExitLink({ to, onClick, ...props }: Omit<ComponentProps<typeof Link>, "to"> & { to: string }) {
  const { locale } = useI18n();
  const navigate = useNavigate();
  const queries = useQueryClient();
  const frontier = useFrontierWorkspace();
  const leave = useDraftLeave();
  const busy = useRef(false);
  async function exit(event: MouseEvent<HTMLAnchorElement>) {
    event.preventDefault();
    if (busy.current || isFrontierExitPending()) return;
    const zh = locale === "zh-CN";
    if (!window.confirm(zh ? "本次 Demo 进度不会保留，是否确认离开？" : "This Demo's progress will not be saved. Leave the Demo?")) return;
    busy.current = true;
    setFrontierExitPending(true);
    try {
      if (frontier) await queries.cancelQueries();
      if (getFrontierToken()) {
        try { await postJSON("/auth/frontier-demo-session/end", {}, { _authScope: "frontier", _skipAuthRefresh: true } as AuthAwareRequestConfig); }
        catch (error) { if (normalizeAPIError(error).status !== 401) throw error; }
      }
      await Promise.all(leave.controllers.map((controller) => controller.remove()));
      leave.discardAndLeave(() => {
        // Unmount the guest guard before its external token store changes.
        // Otherwise its entry redirect can win over the requested public page.
        navigate(to, { replace: true, flushSync: true });
        endFrontierSessionLocally();
        if (frontier) queries.clear();
      });
      onClick?.(event);
    } catch {
      window.alert(zh ? "暂时无法结束 Demo，请重试。" : "The Demo could not be ended. Please retry.");
    } finally { setFrontierExitPending(false); busy.current = false; }
  }
  return <Link {...props} to={to} onClick={(event) => { void exit(event); }} />;
}
