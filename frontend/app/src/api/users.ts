import { postJSON } from "./client";
import type { InviteRequest, InviteResponse } from "@/types";

export function createInvite(request: InviteRequest): Promise<InviteResponse> {
  return postJSON<InviteResponse, InviteRequest>("/users/invite", request);
}
