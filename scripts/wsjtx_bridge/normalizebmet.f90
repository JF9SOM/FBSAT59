! normalizebmet.f90
!
! Hand-extracted copy of the `normalizebmet` subroutine that lives at the
! bottom of WSJT-X's lib/ft8/ft8b.f90 (FT8-only decode driver, which we do
! not otherwise need for FT4). lib/ft4/get_ft4_bitmetrics.f90 calls this
! same subroutine, but it is not defined in any file under lib/ft4/ or
! lib/ft8/ that is safe to compile standalone, so it is vendored here
! verbatim instead of pulling in all of ft8b.f90.
!
! If a future WSJT-X release changes this subroutine's behavior, this copy
! will silently drift out of sync — check ft8b.f90's normalizebmet when
! bumping the pinned WSJT-X tag in build_ft4wsjt.sh.

subroutine normalizebmet(bmet, n)
   real bmet(n)

   bmetav = sum(bmet)/real(n)
   bmet2av = sum(bmet*bmet)/real(n)
   var = bmet2av - bmetav*bmetav
   if (var .gt. 0.0) then
      bmetsig = sqrt(var)
   else
      bmetsig = sqrt(bmet2av)
   endif
   bmet = bmet/bmetsig
   return
end subroutine normalizebmet
