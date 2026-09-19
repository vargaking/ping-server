from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE "tokens" ADD "expires_at" TIMESTAMPTZ;"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE "tokens" DROP COLUMN "expires_at";"""


MODELS_STATE = (
    "eJztXWtv2zYU/SuGP3VAFqRu0hbDMMBOnNVrYg+JshUtCoGWGFuITLoSlcTo8t9HUi/qaU"
    "uWbCnmlyaheCXyXD4OzyXZn90F1qFpH5/PAULQ7P7W+dlFYAHpL/FHR50uWC7DByyBgKnJ"
    "82puJp4IpjaxgEZo+j0wbUiTdGhrlrEkBkY0FTmmyRKxRjMaaBYmOcj44UCV4Bkkc2jRB9"
    "++02QD6fAZ2v6fywf13oCmHimtobNv83SVrJY8bYTIJc/IvjZVNWw6CxRmXq7IHKMgt4EI"
    "S51BBC1AIHs9sRxWfFY6r6Z+jdyShlncIgo2OrwHjkmE6m6IgYYRw4+WxuYVnLGv/Np7e/"
    "rh9OO796cfaRZekiDlw4tbvbDuriFHYKx0X/hzQICbg8MY4sZ/JpCjTrfSofPzx8CjRY6D"
    "50OVh56fEMIXNpmK8FuAZ9WEaEbm9M+3Jyc5aP3Tvzn/1L95Q3P9wmqDaTN22/fYe9Rznz"
    "FIQwg1C7Iqq4AkgbygT4ixgOlgRi1jkOqe6bH/S0MBpnXQJ8hceW0/B19ldD28VfrXf7Oa"
    "LGz7h8kh6itD9qTHU1ex1DfvY64IXtL5d6R86rA/O18n4yFHENtkZvEvhvmUr11WJuAQrC"
    "L8pAJd6KZ+qg9M1LHuqKbakBAKgZ1071+3k3GGa1Ns4w42NNL5r2MadmLgqcq13d/vHaQx"
    "l3amjmHSktjH7LN/dMs7PMfBDI6Ib/1+8+a6/yXepc6vJoO409gLBrHuxeEqMEL5+Xc3Qn"
    "UJfCZbIBofozYaonJGqPgAZUPrEVpqoSkyYrN+pmzIWFTBZMkYxv1D6lzpYpIE8RJb0Jih"
    "z3DFsRzRMgGkpTVCj1PdBi9qHoYvfkvwU8PB0gJPAfOKNhBaRVoxSNyu2b89718MuxzKKd"
    "AenoClqxmYLqBtgxlMGVsHnuXl5xtoAl6NTECv3be0C1GOD+5hAZcIYslHi94ingIQrbfu"
    "fZt9yUNkhB4N7o4Eofee5PJ5g+dpGJ2/uxtdFODzjmPox8ymTKtYT+uFqZV/if1zWtO8yo"
    "f8dy4TEqdLXrt8fi/J6Sslp4/ANHTVQcQwi3o2ZlqBa70C735Qbbon/WrnupLRP8dOmwMz"
    "6ZloUoqd7d5j1SgZIWq0+hQQB6UMbJmwRWx2x2pPmoPaEtj2E6bsYg7seRI5ha5k0qFLGJ"
    "ZaXzVroBh+UfKXq8E4cTUZ/+lnj69howAbtkrZkvGYsmYdYGxCgDI4jGgXw3ZKDetqmwGx"
    "qRrdwWRyFUF3MIrDd3c9GNK1LIeaZnIJa0qj9anIdFVsEZuwO6SFrFQApAKwbwUgvRNXgN"
    "+d3Xb0EmNTUQ2lTvXA11NS5ANBasnWD0RVpzkCgowHro8HMhmjiPTi59+t+FIbJVyrtAjj"
    "GUYEpq0+sjm0YNKW+Omu6bPUq16pXsVAp7P6YlnUrxHDdrq1JW7cTKyCBLDZo0gwXLSRQf"
    "AyQXDqkjkuuICL2BzSAi5tD0cx3SBidKjQSdGgGtHA7Ydy0RsfktZLBlJu2UJuCfftbgme"
    "sAO4vehFh/QmaS032EwVWnj6UZ7KYtEcNUgs34JNvl7/+y5Fl3pFl/1uwq4dvcj2xrNNtj"
    "eeZW9vPEtsbxSLlQAxW4yJmclwptRjDkmPKbOpXW5m33YdL9dUMhDb1K3YjE6yTWXWlpux"
    "GXFVcPsWq7XuxxZAyeD6IWT5jJ9yde6lOoi/5a05+Psl7a+Z9gPbpmNVKXIVM5XsqmHsin"
    "fUQv1CsDikOT62o7cgNRIsDgm0HGLkj+Bb0iJffGoefpuSIqE/rRdL/fn04AV6oUM1SSP1"
    "WHoKcwr5ezZrcvmx3IzWOoIkL6eQl1NIhpml3/FF/9LC90balJ+n4sUtpZa3hZZXTkhNmE"
    "oflPEBfkJF1wyiyQEdncxZM3BEdkd/9xY0W8t+xaYRob+3Q6Uzvru62kxIFW9DKy+jtnLb"
    "RfQoZHiLRHkYwusqWorCId9wEpfEKogstBgBb9avIMTCRloFtzF2VWuQRcEPEHVTVAL3wV"
    "GeSEBYFqkRtE4jIL5rNxUJAoM27p7qnZ1toBHQXJkaAX8mNYKD0Ajg89Kgbyvh2KilvCFo"
    "z4euZGRs+1WujPE0O8aTtTdm/a6YgFBL5ta0/pjH3JjbikZ4RJs28rfqd79L8vZKydvSmZ"
    "qGpj7AlPtycq5pi1jJQw2phxpKBM1ktGzLSM2eLx7cHx+rtVUn+O5G8Qlv4Jf6fFfq89E1"
    "ZkUifSu3/8fjyroqbBwrj8fGKn1z4qIRKEJhvDwGgf7e0ubA+0YlrUFGbjIX/wEsGSKACF"
    "u+GMCOyIQnxKo+JOMLSfJ0/G50ArnCfaUrXHkYVh6UkeGAxocD5I1MGchtde66TjbVh5ah"
    "zdN4lPckl0GBMI+MpzRsQMvjSWxxknoHTnY4RTBpi6q2g/0wrGsUANHL3k4A6zl0lHU1ds"
    "7/l5p5NfYO9PXadN7KlPQCOm/1E8vL/z5WFh0="
)
