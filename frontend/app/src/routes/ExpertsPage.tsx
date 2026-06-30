import { Button } from "@/components/ui/Button";
import { Card, SectionHeader } from "@/components/ui/Card";
import { Field } from "@/components/ui/Field";
import { Input } from "@/components/ui/Input";

export function ExpertsPage() {
  return (
    <div className="grid gap-5">
      <SectionHeader title="BYOK 专家" description="配置教师自己的模型 API key，key 不应落盘或显示回读。" />
      <Card className="max-w-2xl">
        <div className="grid gap-4">
          <Field label="Provider">
            <Input placeholder="openai / gemini / zhipu / anthropic" />
          </Field>
          <Field label="API key">
            <Input placeholder="不会明文显示回读" type="password" />
          </Field>
          <Field label="模型">
            <Input placeholder="例如 gpt-4o-mini" />
          </Field>
          <Button type="button" className="w-fit">
            添加专家
          </Button>
        </div>
      </Card>
    </div>
  );
}

