import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import * as ocrProvidersApi from "@/api/ocrProviders";
import { expertKeys, ocrProviderKeys } from "./keys";

function useInvalidateOCRConfiguration() {
  const queryClient = useQueryClient();
  return () => {
    queryClient.invalidateQueries({ queryKey: ocrProviderKeys.all });
    queryClient.invalidateQueries({ queryKey: expertKeys.stage() });
  };
}

export function useBaiduOCRConfiguration() {
  return useQuery({
    queryKey: ocrProviderKeys.baiduConfiguration(),
    queryFn: ocrProvidersApi.getBaiduOCRConfiguration,
  });
}

export function useSaveBaiduOCRCredentials() {
  const invalidate = useInvalidateOCRConfiguration();
  return useMutation({
    mutationFn: ocrProvidersApi.saveBaiduOCRCredentials,
    onSuccess: invalidate,
  });
}

export function useVerifyBaiduOCRCredentials() {
  const invalidate = useInvalidateOCRConfiguration();
  return useMutation({
    mutationFn: ocrProvidersApi.verifyBaiduOCRCredentials,
    onSettled: invalidate,
  });
}

export function useDeleteBaiduOCRCredentials() {
  const invalidate = useInvalidateOCRConfiguration();
  return useMutation({
    mutationFn: ocrProvidersApi.deleteBaiduOCRCredentials,
    onSuccess: invalidate,
  });
}
