import { useEffect, useRef } from "react";
import { Button } from "@/components/ui/Button";

export function RegistrationDomainDialog({ zh, onClose }: { zh: boolean; onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const element = dialog.current;
    element?.showModal();
    return () => element?.close();
  }, []);

  return (
    <dialog
      ref={dialog}
      onClose={() => {
        // StrictMode reopens the dialog after effect cleanup; ignore that queued close event.
        if (!dialog.current?.open) onClose();
      }}
      aria-labelledby="registration-domain-title"
      aria-describedby="registration-domain-description"
      className="fixed inset-0 m-auto w-[calc(100%-2rem)] max-w-md rounded-xl border bg-card p-6 text-foreground shadow-2xl backdrop:bg-slate-950/40"
    >
      <h2 id="registration-domain-title" className="text-lg font-semibold">
        {zh ? "该邮箱域名暂未开放注册" : "Email domain not yet available"}
      </h2>
      <p id="registration-domain-description" className="mt-3 break-words text-sm leading-6 text-muted-foreground">
        {zh ? "该邮箱域名暂未开放注册，敬请期待。如需咨询，请邮件联系 " : "Registration is not yet available for this email domain. For enquiries, contact "}
        <a className="text-primary underline" href="mailto:smartai-univ@gmail.com">smartai-univ@gmail.com</a>
        {zh ? "。" : "."}
      </p>
      <div className="mt-5 flex justify-end">
        <Button autoFocus onClick={onClose}>{zh ? "返回修改" : "Edit email"}</Button>
      </div>
    </dialog>
  );
}
