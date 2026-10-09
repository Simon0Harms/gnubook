package gnubook.ppcore;

/** An error that is reported to the caller with an HTTP status and a short code. */
final class ApiException extends RuntimeException
{
    private static final long serialVersionUID = 1L;

    final int status;
    final String code;

    ApiException(int status, String code, String message)
    {
        super(message);
        this.status = status;
        this.code = code;
    }

    static ApiException badRequest(String message)
    {
        return new ApiException(400, "bad_request", message);
    }

    static ApiException notFound(String message)
    {
        return new ApiException(404, "not_found", message);
    }

    static ApiException conflict(String code, String message)
    {
        return new ApiException(409, code, message);
    }
}
