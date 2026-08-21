program compensated_sum
    use iso_fortran_env, only: real64
    implicit none

    integer :: count, index, status
    real(real64) :: correction, total, updated, value

    read (*, *, iostat=status) count
    if (status /= 0 .or. count < 1 .or. count > 6) stop 2

    total = 0.0_real64
    correction = 0.0_real64
    do index = 1, count
        read (*, *, iostat=status) value
        if (status /= 0) stop 3

        updated = total + value
        if (abs(total) >= abs(value)) then
            correction = correction + ((total - updated) + value)
        else
            correction = correction + ((value - updated) + total)
        end if
        total = updated
    end do

    write (*, '(ES26.17E3)') total + correction
end program compensated_sum
