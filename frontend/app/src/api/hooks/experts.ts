import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import * as expertsApi from "@/api/experts";
import { expertKeys } from "./keys";

export function useExperts() {
  return useQuery({
    queryKey: expertKeys.list(),
    queryFn: expertsApi.listExperts,
  });
}

export function useProviderCatalog() {
  return useQuery({
    queryKey: expertKeys.catalog(),
    queryFn: expertsApi.listProviderCatalog,
    staleTime: 60 * 60 * 1000,
  });
}

export function useAddExpertKey() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: expertsApi.addExpertKey,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: expertKeys.all });
    },
  });
}

export function useSelectExpert() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ providerId, enabled }: { providerId: string; enabled: boolean }) =>
      expertsApi.selectExpert(providerId, enabled),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: expertKeys.all });
    },
  });
}

export function useUpdateExpert() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({
      providerId,
      request,
    }: {
      providerId: string;
      request: Parameters<typeof expertsApi.updateExpert>[1];
    }) => expertsApi.updateExpert(providerId, request),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: expertKeys.all });
    },
  });
}

export function useVerifyExpert() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: expertsApi.verifyExpert,
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: expertKeys.all });
    },
  });
}

export function useRemoveExpert() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: expertsApi.removeExpert,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: expertKeys.all });
    },
  });
}
