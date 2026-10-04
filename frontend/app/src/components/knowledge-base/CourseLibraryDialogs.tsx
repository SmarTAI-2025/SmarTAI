import { materialMetadataVersion } from "@/lib/editorVersions";
import { useDraftProtection } from "@/hooks/useDraftProtection";
import { useDraftLeave } from "@/hooks/useDraftLeave";
import { FileUp, FolderPlus, LoaderCircle, Trash2 } from "lucide-react";
import { useId, useMemo, useState, type FormEvent, type ReactNode } from "react";
import { toast } from "sonner";
import { getAPIErrorCode, getAPIErrorDetail, normalizeAPIError } from "@/api/client";
import {
  useCreateCourseMaterialGroup,
  useDeleteCourseMaterial,
  useDeleteCourseMaterialGroup,
  useUpdateCourseMaterial,
  useUpdateCourseMaterialGroup,
  useUploadCourseMaterial,
} from "@/api/hooks";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { useI18n } from "@/i18n/I18nProvider";
import {
  isKnowledgeStorageQuotaExceeded,
  knowledgeStorageQuotaCopy,
} from "@/lib/knowledgeStorage";
import type {
  Course,
  CourseMaterial,
  CourseMaterialCategory,
  CourseMaterialGroup,
} from "@/types";
import { LibraryDialog } from "./LibraryDialog";

const SELECT_CLASS = "h-10 w-full rounded-[8px] border bg-card px-3 text-sm outline-none focus:border-primary focus:ring-2 focus:ring-primary/15";

const categories: CourseMaterialCategory[] = ["textbook", "answer", "lecture", "rubric", "other"];

function tx(locale: "zh-CN" | "en-US", zh: string, en: string) {
  return locale === "zh-CN" ? zh : en;
}

function categoryLabel(category: CourseMaterialCategory, locale: "zh-CN" | "en-US") {
  const values = {
    textbook: ["教材", "Textbook"],
    answer: ["课后答案", "Answer key"],
    lecture: ["讲义", "Lecture"],
    rubric: ["评分标准", "Rubric"],
    other: ["其他", "Other"],
  } as const;
  return values[category][locale === "zh-CN" ? 0 : 1];
}

interface GroupDialogProps {
  group?: CourseMaterialGroup;
  courses: Course[];
  onClose: () => void;
  onSaved: (group: CourseMaterialGroup) => void;
  onUseExisting: (groupId: string) => void;
  onDeleted?: () => void;
}

interface SimilarGroupCandidate extends CourseMaterialGroup {
  score?: number;
  reason?: string;
}

export function GroupDialog({ group, courses, onClose, onSaved, onUseExisting, onDeleted }: GroupDialogProps) {
  const { locale } = useI18n();
  const createGroup = useCreateCourseMaterialGroup();
  const updateGroup = useUpdateCourseMaterialGroup();
  const deleteGroup = useDeleteCourseMaterialGroup();
  const [name, setName] = useState(group?.name ?? "");
  const [courseId, setCourseId] = useState(group?.course_id ?? "");
  const leave = useDraftLeave();
  const [similar, setSimilar] = useState<SimilarGroupCandidate[]>([]);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const isPending = createGroup.isPending || updateGroup.isPending || deleteGroup.isPending;

  const localDraft = useDraftProtection({ scope: `library-group:${group?.group_id ?? "new"}`, value: { name, courseId }, baseline: { name: group?.name ?? "", courseId: group?.course_id ?? "" }, version: JSON.stringify([group?.name, group?.course_id]), busy: isPending, onRestore: (draft) => { setName(draft.name); setCourseId(draft.courseId); } });
  async function save(event?: FormEvent, force = false) {
    event?.preventDefault();
    const normalizedName = name.trim();
    if (!normalizedName) return;
    try {
      const result = group
        ? await updateGroup.mutateAsync({
            groupId: group.group_id,
            body: { name: normalizedName, course_id: courseId || null },
          })
        : await createGroup.mutateAsync({
            name: normalizedName,
            course_id: courseId || null,
            force_create: force,
          });
      toast.success(group
        ? tx(locale, "分组已更新", "Group updated")
        : tx(locale, "分组已创建", "Group created"));
      if (!localDraft.isCurrent()) return;
      if (!await localDraft.clear({ name, courseId })) return;
      if (!localDraft.isCurrent()) return;
      onSaved(result);
    } catch (error) {
      const normalized = normalizeAPIError(error);
      const detail = getAPIErrorDetail(normalized);
      if (
        !group
        && normalized.status === 409
        && detail
        && "code" in detail
        && detail.code === "similar_items"
        && "candidates" in detail
        && Array.isArray(detail.candidates)
      ) {
        setSimilar(detail.candidates as SimilarGroupCandidate[]);
        return;
      }
      toast.error(tx(locale, "无法保存分组", "Could not save group"), { description: normalized.message });
    }
  }

  async function remove() {
    if (!group) return;
    try {
      const result = await deleteGroup.mutateAsync(group.group_id);
      toast.success(tx(
        locale,
        `分组已删除，${result.moved_to_ungrouped} 份资料已移到未分组`,
        `Group deleted; ${result.moved_to_ungrouped} files moved to ungrouped`,
      ));
      if (!localDraft.isCurrent()) return;
      await localDraft.clear();
      if (!localDraft.isCurrent()) return;
      (onDeleted ?? onClose)();
    } catch (error) {
      toast.error(tx(locale, "无法删除分组", "Could not delete group"), {
        description: normalizeAPIError(error).message,
      });
    }
  }

  return (
    <LibraryDialog
      title={group ? tx(locale, "管理分组", "Manage group") : tx(locale, "新建分组", "New group")}
      description={tx(locale, "分组只整理资料；删除分组不会删除文件。", "Groups organize files. Deleting a group does not delete files.")}
      closeLabel={tx(locale, "关闭", "Close")}
      onClose={onClose}
      footer={confirmDelete && group ? (
        <>
          <span className="mr-auto text-sm font-medium text-danger">{tx(locale, "确认删除这个分组？", "Delete this group?")}</span>
          <Button type="button" variant="secondary" onClick={() => setConfirmDelete(false)} disabled={isPending}>{tx(locale, "取消", "Cancel")}</Button>
          <Button type="button" variant="danger" onClick={() => void remove()} disabled={isPending}>{deleteGroup.isPending ? <LoaderCircle className="h-4 w-4 animate-spin" /> : null}{tx(locale, "确认删除", "Delete")}</Button>
        </>
      ) : (
        <>
          {group ? <Button type="button" variant="ghost" className="mr-auto text-danger hover:text-danger" onClick={() => setConfirmDelete(true)} disabled={isPending}><Trash2 className="h-4 w-4" />{tx(locale, "删除分组", "Delete group")}</Button> : null}
          <Button type="button" variant="secondary" onClick={() => leave.request(onClose)} disabled={isPending}>{tx(locale, "取消", "Cancel")}</Button>
          <Button type="submit" form="course-material-group-form" disabled={isPending || !name.trim()}>{isPending ? <LoaderCircle className="h-4 w-4 animate-spin" /> : <FolderPlus className="h-4 w-4" />}{group ? tx(locale, "保存", "Save") : tx(locale, "创建分组", "Create group")}</Button>
        </>
      )}
    >
      <form id="course-material-group-form" className="grid gap-4" onSubmit={(event) => void save(event)}>
        <FormField label={tx(locale, "分组名称", "Group name")}>
          <Input className="h-10 w-full" value={name} maxLength={80} onChange={(event) => { setName(event.target.value); setSimilar([]); }} placeholder={tx(locale, "例如：第 7 章课后答案", "For example: Chapter 7 answers")} />
        </FormField>
        <FormField label={tx(locale, "课程（可选）", "Course (optional)")}>
          <select className={SELECT_CLASS} value={courseId} onChange={(event) => setCourseId(event.target.value)}>
            <option value="">{tx(locale, "不限定课程", "No course")}</option>
            {courses.map((course) => <option key={course.id} value={course.id}>{course.name}{course.code ? ` · ${course.code}` : ""}</option>)}
          </select>
        </FormField>

        {similar.length ? (
          <div className="rounded-[8px] border border-amber-200 bg-amber-50/70 p-3 dark:border-amber-900 dark:bg-amber-950/20">
            <p className="text-sm font-semibold text-amber-900 dark:text-amber-200">{tx(locale, "找到名称相近的分组", "Similar groups found")}</p>
            <p className="mt-1 text-xs leading-5 text-amber-800 dark:text-amber-300">{tx(locale, "请选择已有分组，或明确仍然新建。", "Choose an existing group or explicitly create another one.")}</p>
            <div className="mt-2 grid gap-2">
              {similar.map((candidate) => (
                <button key={candidate.group_id} type="button" className="flex items-center justify-between rounded-md border bg-card px-3 py-2 text-left text-sm hover:border-primary" onClick={() => leave.request(() => onUseExisting(candidate.group_id))}>
                  <span className="font-medium">{candidate.name}</span>
                  <span className="text-xs text-muted-foreground">{candidate.material_count} {tx(locale, "份资料", "files")}</span>
                </button>
              ))}
            </div>
            <Button type="button" variant="secondary" className="mt-3 w-full" disabled={isPending} onClick={() => void save(undefined, true)}>{tx(locale, "仍然新建这个分组", "Create this group anyway")}</Button>
          </div>
        ) : null}
      </form>
    </LibraryDialog>
  );
}

interface UploadDialogProps {
  courses: Course[];
  groups: CourseMaterialGroup[];
  onClose: () => void;
  onUploaded: (material: CourseMaterial) => void;
}

export function UploadDialog({ courses, groups, onClose, onUploaded }: UploadDialogProps) {
  const { locale } = useI18n();
  const upload = useUploadCourseMaterial();
  const inputId = useId();
  const [file, setFile] = useState<File | null>(null);
  const [nativeOnly, setNativeOnly] = useState(false);
  const leave = useDraftLeave();
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [courseId, setCourseId] = useState("");
  const [groupId, setGroupId] = useState("");
  const [category, setCategory] = useState<CourseMaterialCategory>("other");
  const [labels, setLabels] = useState("");
  const availableGroups = useMemo(
    () => groups.filter((group) => !courseId || !group.course_id || group.course_id === courseId),
    [courseId, groups],
  );

  const localDraft = useDraftProtection({ scope: "library-upload", value: { file, nativeOnly, courseId, groupId, category, labels }, busy: upload.isPending, onRestore: (draft) => { setFile(draft.file); setNativeOnly(draft.nativeOnly); setCourseId(draft.courseId); setGroupId(draft.groupId); setCategory(draft.category); setLabels(draft.labels); } });
  function selectFile(selected: File | undefined) {
    if (!selected) return;
    const error = selected.size === 0
      ? tx(locale, "文件为空（0 字节），请确认有内容后重新上传。", "The file is empty (0 bytes). Add content before uploading again.")
      : selected.size > 64 * 1024 * 1024
        ? tx(locale, "文件超过 64 MiB，请拆分或压缩后重新上传。", "The file exceeds 64 MiB. Split or compress it before uploading again.")
        : !/\.(pdf|txt|md|markdown)$/i.test(selected.name)
          ? tx(locale, "资料库仅支持 PDF、TXT 和 Markdown。请先导出为支持的格式，不要只修改扩展名。", "The library supports PDF, TXT and Markdown. Export to a supported format; do not just rename the extension.")
          : null;
    setFile(error ? null : selected);
    setUploadError(error);
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!file) return;
    setUploadError(null);
    try {
      const result = await upload.mutateAsync({
        file,
        courseId: courseId || undefined,
        groupId: groupId || undefined,
        category,
        labels: parseLabels(labels),
        nativeOnly,
      });
      toast.success(result.created
        ? result.parse_status === "ready" ? tx(locale, "资料已上传并解析", "Material uploaded and parsed") : tx(locale, "资料已保存，已进入处理队列", "Material saved and queued")
        : tx(locale, "相同资料已存在，已复用原文件", "The same material already exists and was reused"));
      if (!localDraft.isCurrent()) return;
      if (!await localDraft.clear({ file, nativeOnly, courseId, groupId, category, labels })) return;
      if (!localDraft.isCurrent()) return;
      onUploaded(result);
    } catch (error) {
      const normalized = normalizeAPIError(error);
      const quotaExceeded = isKnowledgeStorageQuotaExceeded(normalized);
      const code = getAPIErrorCode(normalized);
      const description = quotaExceeded
        ? knowledgeStorageQuotaCopy(locale).description
        : code === "source_content_type_not_allowed" || code === "invalid_course_material"
          ? tx(locale, "文件内容无法作为资料读取。请确认文件能正常打开，并重新导出为 PDF、TXT 或 Markdown 后上传。", "The file content could not be read as course material. Check that it opens, then export it again as PDF, TXT or Markdown.")
          : code === "material_label_too_long"
            ? tx(locale, "单个标签不能超过 60 个字符，请缩短后再上传。", "Each label must be at most 60 characters. Shorten it before uploading.")
            : code === "too_many_material_labels"
              ? tx(locale, "最多添加 20 个标签，请减少后再上传。", "Use at most 20 labels before uploading.")
              : normalized.message;
      setUploadError(description);
      toast.error(tx(locale, "资料上传失败", "Material upload failed"), { description });
    }
  }

  return (
    <LibraryDialog
      title={tx(locale, "上传资料", "Upload material")}
      description={tx(locale, "PDF / TXT / Markdown · 64 MiB", "PDF / TXT / Markdown · 64 MiB")}
      closeLabel={tx(locale, "关闭", "Close")}
      onClose={onClose}
      footer={<><Button type="button" variant="secondary" onClick={() => leave.request(onClose)} disabled={upload.isPending}>{tx(locale, "取消", "Cancel")}</Button><Button type="submit" form="course-material-upload-form" disabled={upload.isPending || !file}>{upload.isPending ? <LoaderCircle className="h-4 w-4 animate-spin" /> : <FileUp className="h-4 w-4" />}{upload.isPending ? tx(locale, "正在上传…", "Uploading…") : tx(locale, "上传资料", "Upload")}</Button></>}
    >
      <form id="course-material-upload-form" className="grid gap-4" onSubmit={(event) => void submit(event)}>
        <div>
          <label htmlFor={inputId} className="flex min-h-[92px] cursor-pointer flex-col items-center justify-center rounded-[8px] border border-dashed bg-slate-50/60 px-4 py-4 text-center outline-none hover:border-primary dark:bg-slate-900/30">
            <FileUp className="h-5 w-5 text-primary" />
            <span className="mt-2 max-w-full truncate text-sm font-semibold">{file?.name ?? tx(locale, "选择一份资料文件", "Choose a material file")}</span>
            <span className="mt-1 text-xs text-muted-foreground">{file ? formatBytes(file.size) : "PDF / TXT / Markdown"}</span>
          </label>
          <input id={inputId} className="sr-only" type="file" accept=".pdf,.txt,.md,.markdown" aria-invalid={Boolean(uploadError)} aria-describedby={uploadError ? `${inputId}-error` : undefined} onChange={(event) => { selectFile(event.target.files?.[0]); event.target.value = ""; }} />
        </div>
        {uploadError ? <p id={`${inputId}-error`} role="alert" className="text-sm text-danger">{uploadError}</p> : null}
        <label className="flex items-start gap-2 text-sm">
          <input type="checkbox" className="mt-1" checked={nativeOnly} onChange={(event) => setNativeOnly(event.target.checked)} />
          <span>{tx(locale, "仅提取已有文字（不调用模型）", "Existing text only (no model calls)")}</span>
        </label>
        <div className="grid gap-4 sm:grid-cols-2">
          <FormField label={tx(locale, "课程（可选）", "Course (optional)")}>
            <select className={SELECT_CLASS} value={courseId} onChange={(event) => {
              const next = event.target.value;
              setCourseId(next);
              const selected = groups.find((item) => item.group_id === groupId);
              if (selected?.course_id && selected.course_id !== next) setGroupId("");
            }}>
              <option value="">{tx(locale, "不限定课程", "No course")}</option>
              {courses.map((course) => <option key={course.id} value={course.id}>{course.name}{course.code ? ` · ${course.code}` : ""}</option>)}
            </select>
          </FormField>
          <FormField label={tx(locale, "分组（可选）", "Group (optional)")}>
            <select className={SELECT_CLASS} value={groupId} onChange={(event) => {
              const next = event.target.value;
              setGroupId(next);
              const selected = groups.find((item) => item.group_id === next);
              if (selected?.course_id) setCourseId(selected.course_id);
            }}>
              <option value="">{tx(locale, "未分组", "Ungrouped")}</option>
              {availableGroups.map((group) => <option key={group.group_id} value={group.group_id}>{group.name}</option>)}
            </select>
          </FormField>
        </div>
        <div className="grid gap-4 sm:grid-cols-2">
          <FormField label={tx(locale, "资料类型", "Material type")}>
            <select className={SELECT_CLASS} value={category} onChange={(event) => setCategory(event.target.value as CourseMaterialCategory)}>
              {categories.map((item) => <option key={item} value={item}>{categoryLabel(item, locale)}</option>)}
            </select>
          </FormField>
          <FormField label={tx(locale, "标签（逗号分隔）", "Labels (comma separated)")}>
            <Input className="h-10 w-full" value={labels} onChange={(event) => setLabels(event.target.value)} placeholder={tx(locale, "例如：第 7 章，期中", "For example: Chapter 7, midterm")} />
          </FormField>
        </div>
      </form>
    </LibraryDialog>
  );
}

interface MaterialDialogProps {
  material: CourseMaterial;
  courses: Course[];
  groups: CourseMaterialGroup[];
  onClose: () => void;
  onSaved: (material: CourseMaterial) => void;
  onDeleted: () => void;
  initialConfirmDelete?: boolean;
}

export function MaterialDialog({ material, courses, groups, onClose, onSaved, onDeleted, initialConfirmDelete = false }: MaterialDialogProps) {
  const { locale } = useI18n();
  const update = useUpdateCourseMaterial();
  const remove = useDeleteCourseMaterial();
  const leave = useDraftLeave();
  const [filename, setFilename] = useState(material.filename);
  const [courseId, setCourseId] = useState(material.course_id ?? "");
  const [groupId, setGroupId] = useState(material.group_id ?? "");
  const [category, setCategory] = useState(material.category);
  const [labels, setLabels] = useState(material.labels.join(", "));
  const [confirmDelete, setConfirmDelete] = useState(initialConfirmDelete);
  const isPending = update.isPending || remove.isPending;
  const availableGroups = useMemo(
    () => groups.filter((group) => !courseId || !group.course_id || group.course_id === courseId),
    [courseId, groups],
  );

  const localDraft = useDraftProtection({ scope: `library-material:${material.material_id}`, value: { filename, courseId, groupId, category, labels }, baseline: { filename: material.filename, courseId: material.course_id ?? "", groupId: material.group_id ?? "", category: material.category, labels: material.labels.join(", ") }, version: materialMetadataVersion(material), busy: update.isPending || remove.isPending, onRestore: (draft) => { setFilename(draft.filename); setCourseId(draft.courseId); setGroupId(draft.groupId); setCategory(draft.category); setLabels(draft.labels); } });
  async function save(event: FormEvent) {
    event.preventDefault();
    try {
      const result = await update.mutateAsync({
        materialId: material.material_id,
        body: {
          filename: filename.trim(),
          course_id: courseId || null,
          group_id: groupId || null,
          category,
          labels: parseLabels(labels),
        },
      });
      toast.success(tx(locale, "资料信息已更新", "Material updated"));
      if (!localDraft.isCurrent()) return;
      if (!await localDraft.clear({ filename, courseId, groupId, category, labels })) return;
      if (!localDraft.isCurrent()) return;
      onSaved(result);
    } catch (error) {
      toast.error(tx(locale, "无法更新资料", "Could not update material"), {
        description: normalizeAPIError(error).message,
      });
    }
  }

  async function deleteMaterial() {
    try {
      const result = await remove.mutateAsync({
        materialId: material.material_id,
        confirmReferenced: material.task_reference_count > 0,
      });
      if (!localDraft.isCurrent()) return;
      await localDraft.clear();
      if (!localDraft.isCurrent()) return;
      if (result.status === "deletion_pending") {
        toast.success(tx(locale, "资料已移除", "Material removed"), {
          description: tx(
            locale,
            "系统会在后台自动清理原文件；完成前仍计入知识库占用，无需手动重试。",
            "The source file will be cleaned automatically in the background. It remains charged to knowledge storage until cleanup finishes; no manual retry is needed.",
          ),
        });
      } else {
        toast.success(tx(locale, "资料已删除", "Material deleted"));
      }
      onDeleted();
    } catch (error) {
      toast.error(tx(locale, "无法删除资料", "Could not delete material"), {
        description: normalizeAPIError(error).message,
      });
    }
  }

  return (
    <LibraryDialog
      title={confirmDelete ? tx(locale, "删除资料", "Delete material") : tx(locale, "管理资料", "Manage material")}
      description={confirmDelete
        ? tx(locale, "删除后无法恢复，请确认要删除的文件。", "This cannot be undone. Confirm the file you want to delete.")
        : tx(locale, "修改名称、课程、分组和标签；已解析正文保持不变。", "Change its name, course, group and labels. Parsed content stays unchanged.")}
      closeLabel={tx(locale, "关闭", "Close")}
      onClose={onClose}
      footer={confirmDelete ? (
        <>
          <Button type="button" variant="secondary" onClick={() => { if (initialConfirmDelete) onClose(); else setConfirmDelete(false); }} disabled={isPending}>{tx(locale, "取消", "Cancel")}</Button>
          <Button type="button" variant="danger" onClick={() => void deleteMaterial()} disabled={isPending}>{remove.isPending ? <LoaderCircle className="h-4 w-4 animate-spin" /> : null}{tx(locale, "确认删除", "Delete")}</Button>
        </>
      ) : (
        <>
          <Button type="button" variant="ghost" className="mr-auto text-danger hover:text-danger" onClick={() => setConfirmDelete(true)} disabled={isPending}><Trash2 className="h-4 w-4" />{tx(locale, "删除资料", "Delete material")}</Button>
          <Button type="button" variant="secondary" onClick={() => leave.request(onClose)} disabled={isPending}>{tx(locale, "取消", "Cancel")}</Button>
          <Button type="submit" form="course-material-edit-form" disabled={isPending || !filename.trim()}>{update.isPending ? <LoaderCircle className="h-4 w-4 animate-spin" /> : null}{tx(locale, "保存", "Save")}</Button>
        </>
      )}
    >
      {confirmDelete ? (
        <div className="rounded-[10px] border border-red-200 bg-red-50/60 p-4 dark:border-red-900 dark:bg-red-950/20">
          <div className="flex items-start gap-3">
            <span className="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-red-100 text-danger dark:bg-red-950/70">
              <Trash2 aria-hidden="true" className="h-[18px] w-[18px]" />
            </span>
            <div className="min-w-0">
              <p className="truncate text-sm font-semibold">{material.filename}</p>
              <p className="mt-1 text-sm leading-5 text-muted-foreground">
                {material.task_reference_count
                  ? tx(locale, `这份资料被 ${material.task_reference_count} 个任务引用，删除时也会解除这些引用。`, `This material is used by ${material.task_reference_count} tasks. Deleting it will also detach those references.`)
                  : tx(locale, "这份资料目前没有被任务引用。", "This material is not currently used by any task.")}
              </p>
            </div>
          </div>
        </div>
      ) : (
        <form id="course-material-edit-form" className="grid gap-4" onSubmit={(event) => void save(event)}>
          <FormField label={tx(locale, "文件名称", "File name")}>
            <Input className="h-10 w-full" value={filename} maxLength={240} onChange={(event) => setFilename(event.target.value)} />
          </FormField>
          <div className="grid gap-4 sm:grid-cols-2">
            <FormField label={tx(locale, "课程（可选）", "Course (optional)")}>
              <select className={SELECT_CLASS} value={courseId} onChange={(event) => {
                const next = event.target.value;
                setCourseId(next);
                const selected = groups.find((item) => item.group_id === groupId);
                if (selected?.course_id && selected.course_id !== next) setGroupId("");
              }}>
                <option value="">{tx(locale, "不限定课程", "No course")}</option>
                {courses.map((course) => <option key={course.id} value={course.id}>{course.name}{course.code ? ` · ${course.code}` : ""}</option>)}
              </select>
            </FormField>
            <FormField label={tx(locale, "分组（可选）", "Group (optional)")}>
              <select className={SELECT_CLASS} value={groupId} onChange={(event) => {
                const next = event.target.value;
                setGroupId(next);
                const selected = groups.find((item) => item.group_id === next);
                if (selected?.course_id) setCourseId(selected.course_id);
              }}>
                <option value="">{tx(locale, "未分组", "Ungrouped")}</option>
                {availableGroups.map((group) => <option key={group.group_id} value={group.group_id}>{group.name}</option>)}
              </select>
            </FormField>
          </div>
          <div className="grid gap-4 sm:grid-cols-2">
            <FormField label={tx(locale, "资料类型", "Material type")}>
              <select className={SELECT_CLASS} value={category} onChange={(event) => setCategory(event.target.value as CourseMaterialCategory)}>
                {categories.map((item) => <option key={item} value={item}>{categoryLabel(item, locale)}</option>)}
              </select>
            </FormField>
            <FormField label={tx(locale, "标签（逗号分隔）", "Labels (comma separated)")}>
              <Input className="h-10 w-full" value={labels} onChange={(event) => setLabels(event.target.value)} />
            </FormField>
          </div>
          <div className="flex flex-wrap gap-x-5 gap-y-1 rounded-[8px] bg-slate-50 px-3 py-2 text-xs text-muted-foreground dark:bg-slate-900/40">
            <span>{tx(locale, "状态：已解析", "Status: parsed")}</span>
            <span>{formatBytes(material.size_bytes)}</span>
            <span>{material.task_reference_count} {tx(locale, "个任务引用", material.task_reference_count === 1 ? "task reference" : "task references")}</span>
          </div>
        </form>
      )}
    </LibraryDialog>
  );
}

function FormField({ label, children }: { label: string; children: ReactNode }) {
  return <label className="grid gap-1.5 text-sm font-semibold"><span>{label}</span>{children}</label>;
}

function parseLabels(value: string) {
  const seen = new Set<string>();
  return value
    .split(/[,，]/)
    .map((item) => item.trim())
    .filter((item) => {
      const key = item.toLocaleLowerCase();
      if (!item || seen.has(key)) return false;
      seen.add(key);
      return true;
    })
    .slice(0, 20);
}

function formatBytes(value: number) {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}
