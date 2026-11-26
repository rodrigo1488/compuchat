import { useQuery } from "react-query";
import toastError from "../../errors/toastError";
import api from "../../services/api";

const EMPTY_USERS = [];

const useUsers = () => {
  const { data, isLoading } = useQuery(
    ["users"],
    async () => {
      const { data: payload } = await api.get("/users", { params: {} });
      return payload;
    },
    {
      staleTime: 60 * 1000,
      onError: (err) => toastError(err),
    }
  );

  return {
    users: data?.users || EMPTY_USERS,
    hasMore: Boolean(data?.hasMore),
    count: data?.count || 0,
    loading: isLoading,
  };
};

export default useUsers;
