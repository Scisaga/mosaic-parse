import { useQuery } from '@tanstack/react-query'
import { fetchServiceSnapshot } from '../api/client'

export function useServiceHealth() {
  return useQuery({
    queryKey: ['service-health'],
    queryFn: fetchServiceSnapshot,
    refetchInterval: 15_000,
    retry: 1,
    staleTime: 5_000,
  })
}
