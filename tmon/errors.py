class TmonError(Exception):
    def __init__(self, code, message, exit_code=5, retryable=False, request_id=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code
        self.retryable = retryable
        self.request_id = request_id

    def as_dict(self):
        return {"code": self.code, "message": self.message,
                "retryable": self.retryable, "requestId": self.request_id}


def invalid_data(message="공급자 응답의 데이터 형식을 확인할 수 없습니다."):
    return TmonError("invalid-data", message)
